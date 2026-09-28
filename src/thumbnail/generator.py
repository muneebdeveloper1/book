import os
from pathlib import Path
from urllib.parse import quote

import requests
from PIL import Image, ImageDraw, ImageFont, ImageEnhance


def _font(size):
    return ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', size)


def generate_ai_background(prompt, out):
    key = os.getenv('POLLINATIONS_API_KEY', '').strip()
    if not key:
        raise RuntimeError('POLLINATIONS_API_KEY is required for AI thumbnail generation')
    base = os.getenv('POLLINATIONS_BASE_URL', 'https://gen.pollinations.ai').rstrip('/')
    model = os.getenv('POLLINATIONS_IMAGE_MODEL', 'flux')
    r = requests.get(
        f"{base}/image/{quote(prompt, safe='')}",
        headers={'Authorization': f'Bearer {key}'},
        params={'model': model, 'width': 1280, 'height': 720},
        timeout=180,
    )
    r.raise_for_status()
    if 'image' not in r.headers.get('content-type', ''):
        raise RuntimeError('Pollinations thumbnail response was not an image')
    Path(out).write_bytes(r.content)


def create(background, hook, out, size=(1280, 720)):
    im = Image.open(background).convert('RGB').resize(size)
    im = ImageEnhance.Contrast(im).enhance(1.08)
    im = ImageEnhance.Sharpness(im).enhance(1.08)
    overlay = Image.new('RGBA', size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rectangle((0, 430, size[0], size[1]), fill=(0, 0, 0, 170))
    im = Image.alpha_composite(im.convert('RGBA'), overlay)
    d = ImageDraw.Draw(im)
    text = (hook or 'THE TRUTH').upper().strip()[:90]
    font_size = 72
    while font_size > 42 and d.multiline_textbbox((0, 0), text, font=_font(font_size), spacing=5, stroke_width=2)[2] > 1160:
        font_size -= 4
    d.multiline_text(
        (55, 465), text, font=_font(font_size), fill='white', spacing=5,
        stroke_width=3, stroke_fill='black'
    )
    im.convert('RGB').save(out, 'JPEG', quality=94, optimize=True)
