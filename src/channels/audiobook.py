"""Audiobook channel orchestration.

Rights policy (enforced here, not merely prompted for):

* ``reject`` ALWAYS blocks the job. ``AUDIOBOOK_ALLOW_UNKNOWN_RIGHTS`` relaxes
  only the ``unknown`` verdict; it can never unblock a rejection, and a request
  that asks for reproduction is rejected deterministically before the model is
  even consulted.
* the verdict is a conservative content-policy screen, not legal clearance.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from src.channels import topic_queue
from src.channels.common import Runtime, attach_budget, build_runtime, restore_job, target_words
from src.domain.models import Production, RightsResult
from src.errors import ConfigError, InvalidResponseError, RightsBlocked
from src.metadata.generator import generate as generate_metadata
from src.qa.check import validate_video
from src.rendering.audiobook import render_audiobook
from src.rendering.ffmpeg import concat_audio, duration_of
from src.research.engine import build_research, validate_rights
from src.state.checkpoint import Checkpoint
from src.thumbnail.template import choose_background, create_audiobook_thumbnail
from src.utils.io import read_json, write_json
from src.utils.log import bind, get_logger
from src.visuals.audiobook_engine import find_cover, make_video_cover_overlay
from src.visuals.video_library import random_sequence, select_and_download
from src.youtube.client import YouTube, publish

log = get_logger("audiobook")
LARGE_ARTIFACTS = ("videos/*", "render/*", "audio/*.mp3", "narration.mp3", "final.mp4")


def _optional_asset(path: Path, label: str, strict: bool) -> str:
    if path.is_file():
        return str(path)
    message = f"Optional audiobook asset missing: {label} ({path})"
    if strict:
        raise ConfigError(message + ". Set AUDIOBOOK_STRICT_OPTIONAL_ASSETS=false to render without it.")
    log.warning("rendering without optional asset", asset=label, path=str(path))
    return ""


def enforce_rights(rights: RightsResult, allow_unknown: bool) -> None:
    """A rejection is final. Only 'unknown' can be overridden, and only explicitly."""
    if rights.rights_status == "safe":
        return
    if rights.rights_status == "reject":
        raise RightsBlocked(f"Rights gate rejected this topic: {rights.notes or 'no reason given'}")
    if allow_unknown:
        log.warning("proceeding on an UNKNOWN rights verdict because allow_unknown_rights is enabled",
                    notes=rights.notes[:200])
        return
    raise RightsBlocked(f"Rights verdict is 'unknown': {rights.notes or 'insufficient evidence'}. "
                        "Set AUDIOBOOK_ALLOW_UNKNOWN_RIGHTS=true to accept unknown verdicts.")


def produce(root: str = "work", test: bool = False, runtime: Runtime | None = None) -> str:
    rt = runtime or build_runtime("audiobook", root, test)
    cfg = rt.settings.audiobook
    books_root = Path(cfg.books_root)
    if not books_root.is_dir():
        raise ConfigError(f"Audiobook books directory not found: {books_root} "
                          "(expected <asset_root>/books/<Book Name>/topics.txt)")

    if rt.drive:
        for book_dir in sorted(p for p in books_root.iterdir() if p.is_dir()):
            try:
                topic_queue.sync_queue(book_dir, rt.drive)
            except Exception as exc:  # noqa: BLE001 - a queue sync issue must not block production
                log.warning("queue sync failed", book=book_dir.name, error=str(exc)[:200])

    book = topic_queue.pick_book(books_root)
    if not book:
        raise ConfigError(f"No book with a pending topic found under {books_root}")
    topic = topic_queue.claim_book_topic(book, cfg.max_attempts, cfg.stale_hours * 3600)
    if not topic:
        raise ConfigError(f"No claimable topic in {book}")
    job_id = topic_queue.job_id(book, topic)
    job = rt.jobs_dir / job_id
    bind(job=job_id)
    if rt.drive:
        topic_queue.sync_queue(book, rt.drive)
    if not (job / "state.json").is_file():
        restore_job(rt, job_id, job)
    attach_budget(rt, job)
    cp = Checkpoint(job, job_id, "audiobook", rt.drive)
    if rt.drive:
        rt.drive.set_active_job(job_id, channel="audiobook", topic=topic, book=book.name)

    try:
        cover = find_cover(book)

        # ---- source: rights gate and production mode -----------------------
        if not cp.is_done("source"):
            cp.begin("source")
            subject = f"{book.name} - {topic}"
            rights = validate_rights(rt.llm, rt.searcher, subject)
            write_json(job / "rights.json", rights.model_dump())
            enforce_rights(rights, cfg.allow_unknown_rights)
            classification = rt.llm.json(
                f"""Classify the production mode. Return ONLY {{"mode":"summary"|"original","reason":""}}.
A named existing copyrighted book must use "summary" (our own analysis, never its expression).
An original or public-domain subject uses "original". Book: {book.name}. Topic: {topic}.""",
                max_tokens=600, temperature=0.05)
            mode = classification.get("mode") if isinstance(classification, dict) else None
            if mode not in ("summary", "original"):
                raise InvalidResponseError(f"Invalid production-mode classification: {classification}")
            production = Production(channel="audiobook", job_id=job_id, topic=topic, book=book.name,
                                    mode=mode, rights=rights)
            write_json(job / "production.json", production.model_dump())
            shutil.copy2(cover, job / f"cover{cover.suffix.lower()}")
            cp.commit("source", artifacts=["production.json", "rights.json"])
        production = Production.model_validate(read_json(job / "production.json"))
        mode = production.mode
        # Re-assert the gate on every resume: a stored 'reject' must never be
        # walked past just because the stage is already recorded as complete.
        if production.rights:
            enforce_rights(production.rights, cfg.allow_unknown_rights)

        # ---- research -------------------------------------------------------
        if not cp.is_done("research"):
            cp.begin("research")
            write_json(job / "research.json", build_research(rt.llm, rt.searcher, f"{book.name} - {topic}"))
            cp.commit("research", artifacts=["research.json"])
        research = read_json(job / "research.json")

        # ---- script ---------------------------------------------------------
        if not cp.is_done("script"):
            cp.begin("script")
            words = target_words(rt, cfg)
            instruction = (
                "Write an original educational summary/analysis in your own words. Never reproduce the book's "
                "passages, chapter text or wording, and do not act as a substitute for reading it."
                if mode == "summary" else
                "Write an original book-style educational chapter with a clear argument, examples, a practical "
                "framework and a conclusion.")
            script = rt.llm.text(
                f"""{instruction}
Book: {book.name}
Topic: {topic}
Target length: about {words} words.
Structure: hook, why it matters, context, 3-5 core concepts, examples, misconceptions, practical framework, recap.
Natural spoken narration only: no headings, no stage directions, no markdown.
Research: {json.dumps(research, ensure_ascii=False)[:50000]}""",
                max_tokens=max(6000, words * 2), temperature=0.42)
            (job / "final_script.txt").write_text(script, encoding="utf-8")
            chapters = job / "chapters"
            chapters.mkdir(exist_ok=True)
            write_json(chapters / "01.json", {"number": 1, "title": topic, "script": script})
            cp.commit("script", artifacts=["final_script.txt", "chapters/01.json"])
        script = (job / "final_script.txt").read_text(encoding="utf-8")

        # ---- audio ----------------------------------------------------------
        narration = job / "narration.mp3"
        if not cp.is_done("audio"):
            cp.begin("audio")
            from src.narration.engine import generate_all
            from src.narration.tts import TTSProvider

            files = generate_all(TTSProvider(cfg.tts_profile), job / "chapters", job / "audio")
            concat_audio(files, narration)
            cp.commit("audio", artifacts=["narration.mp3"])

        # ---- reusable visuals -----------------------------------------------
        visuals_dir = job / "visuals"
        sequence_path = visuals_dir / "video_sequence.json"
        overlay = visuals_dir / "book_cover_overlay.png"
        if not cp.is_done("visuals"):
            cp.begin("visuals")
            clips_dir, clips, library = select_and_download(rt, job, cfg)
            if not overlay.is_file():
                make_video_cover_overlay(cover, overlay)
            # Fresh randomization per new production; the saved sequence is reused on resume.
            sequence = random_sequence(clips, duration_of(narration))
            write_json(sequence_path, {"library": library, "clips": sequence})
            cp.commit("visuals", artifacts=["visuals/video_library.json", "visuals/video_sequence.json", "visuals/book_cover_overlay.png"])
        library_manifest = read_json(visuals_dir / "video_library.json", {})
        sequence_data = read_json(sequence_path, {})
        sequence = sequence_data.get("clips") if isinstance(sequence_data, dict) else None
        clips_dir = job / "videos"
        if not sequence or not library_manifest:
            raise InvalidResponseError("Audiobook reusable video sequence is missing")
        # Exact planned duration is checked by the renderer before muxing.

        # ---- render ---------------------------------------------------------
        final = job / "final.mp4"
        strict = cfg.strict_optional_assets
        intro = _optional_asset(cfg.asset("intro_video"), "intro_video", strict)
        if not cp.is_done("render"):
            cp.begin("render")
            render_audiobook(
                narration, sequence, clips_dir, final, overlay,
                intro=intro,
                background_music=_optional_asset(cfg.asset("background_music"), "background_music", strict),
                music_volume=cfg.music_volume, width=rt.settings.render.width, height=rt.settings.render.height,
                fps=rt.settings.render.fps, crf=rt.settings.render.crf, preset=rt.settings.render.preset,
                workers=rt.settings.render.chunk_workers)
            cp.commit("render", artifacts=[])

        # ---- qa -------------------------------------------------------------
        if not cp.is_done("qa"):
            cp.begin("qa")
            expected = duration_of(narration) + (duration_of(intro) if intro else 0)
            result = validate_video(final, rt.settings.render.width, rt.settings.render.height, expected)
            write_json(job / "qa.json", result.model_dump())
            if not result.ok:
                raise InvalidResponseError("Audiobook QA failed: " + ", ".join(result.errors))
            cp.commit("qa", artifacts=["qa.json"])

        # ---- package --------------------------------------------------------
        thumbnail = job / "thumbnail.jpg"
        if not cp.is_done("package"):
            cp.begin("package")
            meta = read_json(job / "metadata.json", None) or generate_metadata(
                rt.llm, topic, script, channel="audiobook", book=book.name)
            write_json(job / "metadata.json", meta)
            background = choose_background(cfg)
            _, thumbnail_side = create_audiobook_thumbnail(
                background, cover, meta.get("thumbnail_hook") or topic, thumbnail,
                side=cfg.thumbnail_cover_side,
                cover_box_left=cfg.thumbnail_cover_box_left, cover_box_right=cfg.thumbnail_cover_box_right,
                text_box_left=cfg.thumbnail_text_box_left, text_box_right=cfg.thumbnail_text_box_right,
                min_words=cfg.thumbnail_hook_words_min, max_words=cfg.thumbnail_hook_words_max)
            meta["thumbnail_background"] = str(background)
            meta["thumbnail_cover_side"] = thumbnail_side
            cp.commit("package", artifacts=["metadata.json", "thumbnail.jpg"])
        meta = read_json(job / "metadata.json")

        # ---- upload / thumbnail / verify -------------------------------------
        yt_state_path = job / "youtube_result.json"

        def save_youtube(data: dict) -> None:
            write_json(yt_state_path, data)
            cp.sync()

        if not cp.is_done("upload"):
            cp.begin("upload")
            yt = YouTube("YOUTUBE_TOKEN_JSON_BOOKS", rt.settings, rt.budget, rt.env)
            publish(yt, job_id, final, thumbnail, meta,
                    rt.env.get("YOUTUBE_VISIBILITY_BOOKS", "private"), yt_state_path, save_youtube,
                    existing=read_json(yt_state_path, None))
            cp.commit("upload", artifacts=["youtube_result.json"])
        youtube_state = read_json(yt_state_path)
        if not cp.is_done("thumbnail"):
            cp.begin("thumbnail")
            cp.commit("thumbnail", artifacts=[])
        if not cp.is_done("verify"):
            cp.begin("verify")
            cp.commit("verify", artifacts=[])

        # ---- cleanup ---------------------------------------------------------
        if not cp.is_done("cleanup"):
            cp.begin("cleanup")
            topic_queue.mark_done(book, topic)
            if rt.drive:
                topic_queue.sync_queue(book, rt.drive)
                rt.drive.clear_active_job(job_id)
                removed = rt.drive.delete_remote(job_id, LARGE_ARTIFACTS)
                log.info("removed regenerable media from Drive", files=removed)
            cp.commit("cleanup", artifacts=[])
        cp.complete(video_id=youtube_state.get("video_id"))
        log.info("audiobook job complete", video_id=youtube_state.get("video_id"))
        return job_id
    except Exception as exc:
        permanent = isinstance(exc, (RightsBlocked, ConfigError))
        status = topic_queue.mark_failed(book, topic, exc, cfg.max_attempts)
        if rt.drive:
            try:
                topic_queue.sync_queue(book, rt.drive)
            except Exception as sync_exc:  # noqa: BLE001 - never mask the real failure
                log.warning("queue sync after failure failed", error=str(sync_exc)[:160])
        log.error("audiobook job failed", error=str(exc)[:300], topic_status=status)
        cp.fail(exc, permanent=permanent)
        raise
