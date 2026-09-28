"""Audiobook thumbnail composition: plain background + straight cover + 3-5 word hook."""
from __future__ import annotations

import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from src.errors import ConfigError

SIZE=(1280,720)
FONT_PATHS=("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf","/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf","/System/Library/Fonts/Supplemental/Arial Bold.ttf","C:\\Windows\\Fonts\\arialbd.ttf")
EXTS={".jpg",".jpeg",".png",".webp"}

def font(size:int):
    for path in FONT_PATHS:
        if Path(path).is_file(): return ImageFont.truetype(path,size)
    return ImageFont.load_default()

def _wrap(draw,text,size,max_width):
    words=text.split(); lines=[]; current=""
    for word in words:
        trial=f"{current} {word}".strip()
        if not current or draw.textlength(trial,font=font(size))<=max_width: current=trial
        else: lines.append(current); current=word
    if current: lines.append(current)
    return "\n".join(lines)

def choose_background(cfg):
    directory=cfg.asset("thumbnail_backgrounds_dir")
    candidates=sorted(p for p in directory.glob("*") if p.suffix.lower() in EXTS) if directory.is_dir() else []
    if candidates: return random.SystemRandom().choice(candidates)
    template=cfg.asset("thumbnail_template")
    if template.is_file(): return template
    raise ConfigError("No audiobook thumbnail background found. Add 2-3 images to assets/audiobook/thumbnail_backgrounds/")

def _valid_hook(hook,min_words,max_words,fallback):
    words=[w for w in (hook or "").upper().split() if w]
    if len(words)<min_words or len(words)>max_words: words=[w for w in fallback.upper().split() if w][:max_words]
    if len(words)<min_words: words=(words+["THE","BIG","IDEA"])[:max_words]
    return " ".join(words)

def create_audiobook_thumbnail(background, cover, hook, out, side="random",
                               cover_box_left=(60,70,500,650), cover_box_right=(780,70,1220,650),
                               text_box_left=(560,90,1230,640), text_box_right=(50,90,720,640),
                               min_words=3,max_words=5):
    background,cover,out=Path(background),Path(cover),Path(out)
    if not background.is_file() or not cover.is_file(): raise ConfigError("Thumbnail background or cover is missing")
    if side=="random": side=random.SystemRandom().choice(["left","right"])
    cb=cover_box_left if side=="left" else cover_box_right
    tb=text_box_left if side=="left" else text_box_right
    canvas=Image.open(background).convert("RGB").resize(SIZE,Image.Resampling.LANCZOS).convert("RGBA")
    art=Image.open(cover).convert("RGB")
    # Straight/front-facing: no rotation or perspective transformation.
    art.thumbnail((cb[2]-cb[0],cb[3]-cb[1]),Image.Resampling.LANCZOS)
    x=cb[0]+(cb[2]-cb[0]-art.width)//2; y=cb[1]+(cb[3]-cb[1]-art.height)//2
    canvas.alpha_composite(art.convert("RGBA"),(x,y))
    draw=ImageDraw.Draw(canvas)
    text=_valid_hook(hook,min_words,max_words,"THE BIG IDEA")
    maxw=tb[2]-tb[0]; maxh=tb[3]-tb[1]; size=88
    while size>34:
        wrapped=_wrap(draw,text,size,maxw); box=draw.multiline_textbbox((0,0),wrapped,font=font(size),spacing=8,stroke_width=4)
        if box[2]-box[0]<=maxw and box[3]-box[1]<=maxh: break
        size-=4
    # subtle dark translucent panel only behind the hook, not over the cover.
    panel=Image.new("RGBA",SIZE,(0,0,0,0)); pd=ImageDraw.Draw(panel); pd.rounded_rectangle((tb[0]-20,tb[1]-20,tb[2]+10,tb[3]+10),radius=18,fill=(0,0,0,105))
    canvas=Image.alpha_composite(canvas,panel); draw=ImageDraw.Draw(canvas)
    draw.multiline_text((tb[0],tb[1]),wrapped,font=font(size),fill="white",spacing=8,stroke_width=4,stroke_fill="black")
    out.parent.mkdir(parents=True,exist_ok=True); canvas.convert("RGB").save(out,"JPEG",quality=95,optimize=True)
    return out,side
