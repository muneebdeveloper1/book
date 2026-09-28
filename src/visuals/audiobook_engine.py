"""Audiobook cover handling and reusable-video visual pipeline."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageFilter, ImageDraw

from src.errors import ConfigError
from src.utils.log import get_logger

log = get_logger("visuals-audiobook")
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "cover.webp")
CANVAS = (1920, 1080)


def find_cover(book) -> Path:
    book = Path(book)
    for name in COVER_NAMES:
        candidate = book / name
        if candidate.is_file():
            try:
                with Image.open(candidate) as im:
                    im.verify()
                return candidate
            except Exception:
                log.warning("ignoring unreadable cover", path=str(candidate))
    raise ConfigError(f"No valid book cover (cover.jpg/.png) found in {book}")


def make_video_cover_overlay(cover: Path, out: Path, top_left=(728, 201), top_right=(1169, 162),
                             bottom_right=(1169, 930), bottom_left=(728, 928)) -> Path:
    """Create a transparent 1920x1080 overlay using the fixed reference perspective.

    The source cover is a plain 2D front. The quadrilateral is the fixed placement
    measured from the supplied 16:9 reference; rendering then reuses this exact
    overlay on every library-video segment.
    """
    import numpy as np
    with Image.open(cover) as im:
        source = im.convert("RGB")
    w, h = source.size
    src_pts = np.float64([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
    dst_pts = np.float64([top_left, top_right, bottom_right, bottom_left])
    # Pillow's PERSPECTIVE transform maps output pixels back into the source.
    a=[]; b=[]
    for (dx,dy),(sx,sy) in zip(dst_pts,src_pts):
        a.append([dx,dy,1,0,0,0,-sx*dx,-sx*dy]); b.append(sx)
        a.append([0,0,0,dx,dy,1,-sy*dx,-sy*dy]); b.append(sy)
    coeffs=np.linalg.solve(np.asarray(a),np.asarray(b))
    warped=source.transform(CANVAS,Image.Transform.PERSPECTIVE,coeffs,Image.Resampling.BICUBIC).convert("RGBA")
    mask_source=Image.new("L",(w,h),255)
    mask=mask_source.transform(CANVAS,Image.Transform.PERSPECTIVE,coeffs,Image.Resampling.BICUBIC)
    warped.putalpha(mask)
    # Soft shadow derived from the exact transformed cover mask.
    shadow_mask=mask.filter(ImageFilter.GaussianBlur(16))
    shadow = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    black = Image.new("RGBA", CANVAS, (0, 0, 0, 125))
    shadow.paste(black, (14, 18), shadow_mask)
    base = shadow
    base.alpha_composite(warped)
    out.parent.mkdir(parents=True, exist_ok=True)
    base.save(out, "PNG", optimize=True)
    return out
