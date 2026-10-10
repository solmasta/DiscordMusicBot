"""Generates the bot's icon and profile banner into assets/. Run: python make_branding.py"""
import math
import os
import random

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
FONT_DIRS = ("/usr/share/fonts/truetype/freefont", "/usr/share/fonts/truetype/dejavu")
CRIMSON, EMBER, HOT = (214, 28, 52), (255, 98, 40), (255, 190, 90)


def font(size: int, name="FreeSansBoldOblique.ttf"):
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def lerp(a, b, t):
    return tuple(int(x + (y - x) * t) for x, y in zip(a, b))


def glow(layer: Image.Image, radius: int, strength: float = 1.0) -> Image.Image:
    g = layer.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        a = g.getchannel("A").point(lambda v: min(255, int(v * strength)))
        g.putalpha(a)
    return g


def grain(img: Image.Image, amount: float = 5.0) -> Image.Image:
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    rng = np.random.default_rng(7)
    arr += rng.normal(0, amount, arr.shape[:2])[..., None]
    return Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).convert("RGBA")


def radial(size: int, inner, outer, power: float = 1.6) -> Image.Image:
    y, x = np.mgrid[0:size, 0:size].astype(np.float32)
    d = np.clip(np.hypot(x - size / 2, y - size / 2) / (size * 0.72), 0, 1) ** power
    out = np.zeros((size, size, 3), dtype=np.float32)
    for i in range(3):
        out[..., i] = inner[i] + (outer[i] - inner[i]) * d
    return Image.fromarray(out.astype("uint8")).convert("RGBA")


def bar_gradient(w: int, h: int, top, bottom) -> Image.Image:
    col = Image.new("RGBA", (w, h))
    px = ImageDraw.Draw(col)
    for y in range(h):
        px.line([(0, y), (w, y)], fill=lerp(top, bottom, y / max(1, h - 1)) + (255,))
    return col


def wordmark(text_parts, size: int, scale_x: float = 0.9):
    """Bold oblique wordmark, slightly condensed. text_parts = [(text, colour), ...]."""
    f = font(size)
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    widths = [probe.textlength(t, font=f) for t, _ in text_parts]
    img = Image.new("RGBA", (int(sum(widths)) + 40, int(size * 1.4)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 20
    for (t, c), w in zip(text_parts, widths):
        d.text((x, 10), t, font=f, fill=c)
        x += w
    bbox = img.getbbox()
    img = img.crop(bbox)
    return img.resize((int(img.width * scale_x), img.height), Image.LANCZOS)


def make_icon(path: str):
    S = 1024
    base = radial(S, (88, 12, 26), (6, 4, 8))
    d = ImageDraw.Draw(base)
    d.ellipse((46, 46, S - 46, S - 46), outline=CRIMSON + (255,), width=10)

    heights = [0.22, 0.38, 0.58, 0.8, 0.95, 1.0, 0.95, 0.8, 0.58, 0.38, 0.22]
    bw, gap, max_h, cy = 54, 22, 400, 420
    total = len(heights) * bw + (len(heights) - 1) * gap
    x0 = (S - total) // 2
    bars = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    for i, hv in enumerate(heights):
        h = int(max_h * hv)
        x = x0 + i * (bw + gap)
        tile = bar_gradient(bw, h, HOT if hv > 0.9 else EMBER, CRIMSON)
        m = Image.new("L", (bw, h), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, bw - 1, h - 1), 26, fill=255)
        bars.paste(tile, (x, cy - h // 2), m)
    base.alpha_composite(glow(bars, 30, 1.6))
    base.alpha_composite(bars)

    # sized to sit inside the red ring so Discord's circular crop never clips the lettering
    mark = wordmark([("CRÜE ", (255, 255, 255, 255)), ("FM", EMBER + (255,))], 158)
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mx, my = (S - mark.width) // 2, 700
    sh = Image.new("RGBA", mark.size, (0, 0, 0, 0))
    sh.putalpha(mark.getchannel("A"))
    shadow.paste((0, 0, 0, 255), (mx + 6, my + 8), sh)
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    base.alpha_composite(mark, (mx, my))
    base = grain(base, 3.0)
    base.convert("RGB").resize((512, 512), Image.LANCZOS).save(path, "PNG", optimize=True)


def make_banner(path: str):
    W, H, SS = 1360, 480, 2
    w, h = W * SS, H * SS
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    # dark stage with a crimson wash from the left and an ember wash from the right
    left = np.exp(-(((x - w * 0.12) / (w * 0.42)) ** 2 + ((y - h * 0.55) / (h * 0.9)) ** 2))
    right = np.exp(-(((x - w * 0.9) / (w * 0.38)) ** 2 + ((y - h * 0.9) / (h * 0.8)) ** 2))
    arr = np.zeros((h, w, 3), dtype=np.float32) + np.array([8, 6, 12], dtype=np.float32)
    arr += left[..., None] * np.array([92, 10, 28], dtype=np.float32)
    arr += right[..., None] * np.array([70, 26, 8], dtype=np.float32)
    vig = np.clip(np.hypot((x - w / 2) / (w * 0.62), (y - h / 2) / (h * 0.95)), 0, 1) ** 2.2
    arr *= (1 - 0.55 * vig)[..., None]
    base = Image.fromarray(np.clip(arr, 0, 255).astype("uint8")).convert("RGBA")

    # equalizer skyline across the bottom, tallest in the middle
    rnd = random.Random(11)
    n, bw, gap = 74, 14 * SS, 4 * SS
    total = n * bw + (n - 1) * gap
    x0 = (w - total) // 2
    bars = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    base_y = h - 14 * SS
    for i in range(n):
        t = i / (n - 1)
        envelope = 0.35 + 0.65 * math.sin(math.pi * t) ** 1.4
        bump = 0.55 + 0.45 * math.sin(i * 0.9) * math.cos(i * 0.37)
        hv = max(0.08, min(1.0, envelope * (0.55 + 0.45 * rnd.random()) * bump + 0.05))
        bh = int(hv * 215 * SS)
        px = x0 + i * (bw + gap)
        tile = bar_gradient(bw, bh, lerp(EMBER, HOT, hv), CRIMSON)
        m = Image.new("L", (bw, bh), 0)
        ImageDraw.Draw(m).rounded_rectangle((0, 0, bw - 1, bh - 1), 6 * SS, fill=255)
        bars.paste(tile, (px, base_y - bh), m)
    base.alpha_composite(glow(bars, 16 * SS, 1.5))
    base.alpha_composite(bars)

    # wordmark + tagline, centred
    mark = wordmark([("CRÜE ", (255, 255, 255, 255)), ("FM", EMBER + (255,))], 250 * SS // 2 * 1)
    mark = mark.resize((int(mark.width * 0.98), int(mark.height * 0.98)), Image.LANCZOS)
    mx, my = (w - mark.width) // 2, int(h * 0.15)
    halo = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse((mx - 120, my - 30, mx + mark.width + 120, my + mark.height + 40), fill=(0, 0, 0, 150))
    base.alpha_composite(halo.filter(ImageFilter.GaussianBlur(60)))
    redglow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    redglow.paste(CRIMSON + (255,), (mx, my), mark.getchannel("A"))
    base.alpha_composite(glow(redglow, 26 * SS, 1.2))
    base.alpha_composite(mark, (mx, my))

    tag_font = font(30 * SS // 2 * 1, "FreeSansBold.ttf")
    d = ImageDraw.Draw(base)
    tagline = "R O C K   R A D I O   ·   W E   S K I P   T H E   C O M M E R C I A L S"
    tw = d.textlength(tagline, font=tag_font)
    ty = my + mark.height + 34 * SS
    d.text(((w - tw) / 2, ty), tagline, font=tag_font, fill=(236, 226, 228, 235))
    d.line([((w - tw) / 2, ty - 14 * SS), ((w + tw) / 2, ty - 14 * SS)], fill=CRIMSON + (255,), width=3 * SS)

    base = grain(base, 3.2)
    base.convert("RGB").resize((W, H), Image.LANCZOS).save(path, "PNG", optimize=True)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    make_icon(os.path.join(OUT, "icon.png"))
    make_banner(os.path.join(OUT, "banner.png"))
    print("wrote", os.listdir(OUT))
