"""Audiobook renderer using reusable Drive video clips."""
from __future__ import annotations

from pathlib import Path

from src.rendering.ffmpeg import RenderError, duration_of, mux, run
from src.utils.log import get_logger

log = get_logger("render-audiobook")


def _concat_manifest(files: list[Path], manifest: Path) -> None:
    manifest.write_text("\n".join("file '" + str(Path(f).resolve()).replace("'", "'\\''") + "'" for f in files) + "\n", encoding="utf-8")


def _normalize_clip(source: Path, out: Path, width: int, height: int, fps: int, crf: int, preset: str) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp=out.with_suffix('.part.mp4')
    run(["ffmpeg","-y","-v","error","-i",str(source),"-vf",
         f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p",
         "-an","-c:v","libx264","-preset",preset,"-crf",str(crf),"-r",str(fps),"-pix_fmt","yuv420p",str(tmp)],
        f"normalize library clip {source.name}")
    tmp.replace(out); return out


def _composite(base: Path, out: Path, width: int, height: int, fps: int, crf: int, preset: str,
               background: str = "", avatar: str = "", avatar_position: str = "top_right",
               avatar_width: int = 280, background_opacity: float = 0.08) -> Path:
    if not background and not avatar:
        if Path(out) != Path(base):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(base).replace(out)
            return Path(out)
        return base
    inputs=["-i",str(base)]; filters=[]; label="[0:v]"; idx=1
    if background and Path(background).is_file():
        inputs += ["-stream_loop","-1","-i",background]
        filters.append(f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps={fps}[bgv]")
        filters.append(f"[bgv]{label}blend=all_mode=softlight:all_opacity={float(background_opacity):.3f}:shortest=1[comp]")
        label="[comp]"; idx+=1
    if avatar and Path(avatar).is_file():
        inputs += ["-stream_loop","-1","-i",avatar]
        pos={"top_right":f"W-w-28:28","top_left":"28:28","bottom_right":"W-w-28:H-h-28","bottom_left":"28:H-h-28"}.get(avatar_position,"W-w-28:28")
        filters.append(f"[{idx}:v]scale={int(avatar_width)}:-2[av]")
        filters.append(f"{label}[av]overlay={pos}:shortest=1[vout]")
        label="[vout]"; idx+=1
    run(["ffmpeg","-y","-v","error",*inputs,"-filter_complex",";".join(filters),"-map",label,"-an","-c:v","libx264","-preset",preset,"-crf",str(crf),"-r",str(fps),"-pix_fmt","yuv420p",str(out)],"audiobook composite")
    return out


def _normalize_intro(source: Path, out: Path, width: int, height: int, fps: int, crf: int, preset: str) -> Path:
    tmp=out.with_suffix('.part.mp4')
    args=["ffmpeg","-y","-v","error","-i",str(source),"-vf",
         f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p",
         "-c:v","libx264","-preset",preset,"-crf",str(crf),"-r",str(fps),"-pix_fmt","yuv420p"]
    # The concat demuxer requires matching stream layouts. Give a silent stereo track
    # to an intro that has no audio, while preserving real intro audio when present.
    from src.rendering.ffmpeg import has_audio
    if has_audio(source):
        args += ["-c:a","aac","-b:a","192k","-ar","48000","-ac","2"]
    else:
        args = ["-i",str(source),"-f","lavfi","-i","anullsrc=channel_layout=stereo:sample_rate=48000","-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p",
                "-shortest","-c:v","libx264","-preset",preset,"-crf",str(crf),"-r",str(fps),"-pix_fmt","yuv420p","-c:a","aac","-b:a","192k"]
    args += [str(tmp)]
    run(args,"normalize intro")
    tmp.replace(out); return out


def _render_sequence(sequence, clips_dir: Path, cover_overlay: Path, out: Path, width, height, fps, crf, preset) -> Path:
    work=out.parent/"render"; norm=work/"library_norm"; chunks=work/"chunks"
    norm.mkdir(parents=True,exist_ok=True); chunks.mkdir(parents=True,exist_ok=True)
    rendered=[]; cache={}; remaining=sum(float(x.get("duration",0)) for x in sequence); target_frames=round(remaining*fps); emitted=0
    for i,item in enumerate(sequence):
        src=clips_dir/item["asset"]
        if not src.is_file(): raise RenderError(f"Library clip missing: {src}")
        normalized=cache.get(src.name) or norm/(src.stem+".mp4")
        if not normalized.is_file(): _normalize_clip(src,normalized,width,height,fps,crf,preset)
        cache[src.name]=normalized
        chunk=chunks/f"seg_{i:05d}.mp4"; tmp=chunk.with_suffix('.part.mp4')
        seconds=float(item["duration"]); frames=max(1,target_frames-emitted) if i==len(sequence)-1 else max(1,round(seconds*fps)); emitted+=frames
        run(["ffmpeg","-y","-v","error","-stream_loop","-1","-i",str(normalized),"-i",str(cover_overlay),
             "-filter_complex","[0:v][1:v]overlay=0:0:format=auto[v]","-map","[v]","-frames:v",str(frames),"-an",
             "-c:v","libx264","-preset",preset,"-crf",str(crf),"-r",str(fps),"-pix_fmt","yuv420p",str(tmp)],f"render audiobook clip {i+1}")
        tmp.replace(chunk); rendered.append(chunk)
    manifest=work/"sequence.concat.txt"; _concat_manifest(rendered,manifest); silent=work/"silent.mp4"
    try: run(["ffmpeg","-y","-v","error","-f","concat","-safe","0","-i",str(manifest),"-c","copy","-movflags","+faststart",str(silent)],"concat randomized audiobook video")
    finally: manifest.unlink(missing_ok=True)
    silent.replace(out); return out


def render_audiobook(audio, sequence, clips_dir, out, cover_overlay, intro="", avatar="", background="", background_music="",
                     music_volume=0.08,width=1920,height=1080,fps=30,crf=20,preset="veryfast",avatar_position="top_right",
                     avatar_width=280,background_volume=0.08,workers=1,work_dir=None):
    out=Path(out); clips_dir=Path(clips_dir); cover_overlay=Path(cover_overlay); work=out.parent/"render"; work.mkdir(parents=True,exist_ok=True)
    total=duration_of(audio); planned=sum(float(x.get("duration",0)) for x in sequence)
    if abs(planned-total)>0.05: raise RenderError(f"Video sequence duration {planned:.3f}s does not match narration {total:.3f}s")
    body_silent=work/"library_video.mp4"
    _render_sequence(sequence,clips_dir,cover_overlay,body_silent,width,height,fps,crf,preset)
    composited=work/"composited.mp4"
    _composite(body_silent,composited,width,height,fps,crf,preset,background,avatar,avatar_position,avatar_width,background_volume)
    body=work/"body.mp4"
    mux(composited,audio,body,background_music or None,music_volume,crf,preset)
    if intro and Path(intro).is_file():
        intro_norm=_normalize_intro(Path(intro),work/"intro_norm.mp4",width,height,fps,crf,preset)
        manifest=work/"final.concat.txt"; _concat_manifest([intro_norm,body],manifest)
        try: run(["ffmpeg","-y","-v","error","-f","concat","-safe","0","-i",str(manifest),"-c","copy","-movflags","+faststart",str(out)],"intro + audiobook concat")
        finally: manifest.unlink(missing_ok=True)
    else:
        body.replace(out)
    log.info("audiobook rendered",seconds=round(duration_of(out),2),clips=len(sequence))
    return out
