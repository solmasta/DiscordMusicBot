"""Now Playing card graphics: a live audio spectrum tap and an animated banner renderer."""
import collections
import colorsys
import io
import math
import os

import discord
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

BANDS = 32
SAMPLES_PER_FRAME = 960          # 20 ms of 48 kHz audio
FFT_SIZE = 4096                  # ~85 ms window: enough resolution to tell the bass bands apart
FRAMES_PER_SLICE = 5             # one spectrum slice every 100 ms
W, H = 800, 300
DEFAULT_ACCENT = (230, 57, 70)

_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
)


class SpectrumTap(discord.AudioSource):
    """Passes audio through untouched while recording what it sounds like (32 band levels every
    100 ms), so the banner's equalizer moves with the real music."""

    def __init__(self, source: discord.AudioSource):
        self.source = source
        self.history: collections.deque = collections.deque(maxlen=80)
        self._window = np.hanning(FFT_SIZE)
        self._freqs = np.fft.rfftfreq(FFT_SIZE, 1 / 48000)
        edges = np.geomspace(50, 11000, BANDS + 1)
        self._centers = np.sqrt(edges[:-1] * edges[1:])
        self._slices = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            a = int(np.searchsorted(self._freqs, lo))
            b = max(a + 1, int(np.searchsorted(self._freqs, hi)))
            self._slices.append((a, b))
        self._wide = np.array([b - a >= 2 for a, b in self._slices])
        self._buf = np.zeros(FFT_SIZE, dtype=np.float32)
        self._acc = np.zeros(BANDS)
        self._n = 0
        # Per-band running loudest/quietest level (dB). Bass is ~35 dB hotter than treble, so each
        # band is scaled against its own range, which keeps every bar moving.
        self._hi: np.ndarray | None = None
        self._lo: np.ndarray | None = None

    def read(self) -> bytes:
        data = self.source.read()
        if data:
            try:
                self._analyse(data)
            except Exception:
                pass   # never let analysis interfere with playback
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        self.source.cleanup()

    def _analyse(self, data: bytes):
        pcm = np.frombuffer(data, dtype=np.int16)
        if pcm.size < SAMPLES_PER_FRAME * 2:
            return
        mono = pcm[: SAMPLES_PER_FRAME * 2].reshape(-1, 2).mean(axis=1)
        self._buf = np.concatenate((self._buf[SAMPLES_PER_FRAME:], mono))
        self._n += 1
        if self._n >= FRAMES_PER_SLICE:
            spec = np.abs(np.fft.rfft(self._buf * self._window))
            means = np.array([spec[a:b].mean() for a, b in self._slices])
            self._acc = np.where(self._wide, means, np.interp(self._centers, self._freqs, spec))
            db = 20 * np.log10(self._acc + 1)
            if self._hi is None:   # start from what the stream really sounds like
                self._hi, self._lo = db + 4, db - 22
            self._hi = np.maximum(self._hi - 0.4, db)
            self._lo = np.minimum(self._lo + 0.4, db)
            rng = np.maximum(self._hi - self._lo, 16.0)
            vals = np.clip((db - self._lo) / rng, 0, 1) ** 1.6
            self.history.append(vals.tolist())
            self._n = 0


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_PATHS:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _accent(art: Image.Image) -> tuple[int, int, int]:
    """A vivid, bright colour from the album art for the bars and highlights."""
    small = art.convert("RGB").resize((64, 64)).quantize(colors=6)
    pal = small.getpalette() or []
    best, best_score = DEFAULT_ACCENT, -1.0
    for count, idx in small.getcolors() or []:
        r, g, b = pal[idx * 3: idx * 3 + 3]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        score = count * (0.25 + s) * (0.25 + v)
        if score > best_score:
            best_score, best = score, (h, s, v)
    if isinstance(best, tuple) and len(best) == 3 and isinstance(best[0], float):
        h, s, v = best
        r, g, b = colorsys.hsv_to_rgb(h, max(s, 0.55), max(v, 0.85))
        return int(r * 255), int(g * 255), int(b * 255)
    return DEFAULT_ACCENT


def _smooth(frames: list[list[float]]) -> list[list[float]]:
    """Fast attack, slow release, like a real equalizer."""
    out, prev = [], [0.0] * BANDS
    for f in frames:
        cur = [p + (v - p) * (0.75 if v > p else 0.28) for p, v in zip(prev, f)]
        out.append(cur)
        prev = cur
    return out


def _idle_frames(n: int) -> list[list[float]]:
    return [
        [0.25 + 0.2 * math.sin(i * 0.5 + b * 0.55) * math.sin(b * 0.2 + i * 0.13) for b in range(BANDS)]
        for i in range(n)
    ]


def render_banner(
    art: bytes | None,
    station: str,
    title: str,
    artist: str,
    progress: float | None,
    spectrum: list[list[float]],
    commercial: bool = False,
    note: str = "",
) -> tuple[bytes, tuple[int, int, int]]:
    """Return (animated GIF bytes, accent colour)."""
    art_img = None
    if art:
        try:
            art_img = Image.open(io.BytesIO(art)).convert("RGB")
        except Exception:
            art_img = None
    accent = _accent(art_img) if art_img else DEFAULT_ACCENT

    # --- background: blurred, darkened album art (or a tinted gradient)
    if art_img:
        bg = ImageOps.fit(art_img, (W, H)).filter(ImageFilter.GaussianBlur(26))
        bg = ImageEnhance.Brightness(bg).enhance(0.32 if not commercial else 0.2)
    else:
        bg = Image.new("RGB", (W, H))
        px = ImageDraw.Draw(bg)
        for y in range(H):
            t = y / H
            px.line([(0, y), (W, y)], fill=(int(16 + accent[0] * 0.10 * (1 - t)), int(16 + accent[1] * 0.10 * (1 - t)), int(26 + 10 * t)))
    shade = Image.new("L", (1, H))
    for y in range(H):
        shade.putpixel((0, y), int(150 * max(0.0, (y - 150) / 150)))
    bg.paste((0, 0, 0), (0, 0), shade.resize((W, H)))
    base = bg.convert("RGBA")

    # --- album art tile with a soft shadow
    tile = 216
    ax, ay = 36, 42
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((ax + 4, ay + 10, ax + tile + 4, ay + tile + 10), 26, fill=(0, 0, 0, 170))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12)))
    if art_img:
        cover = ImageOps.fit(art_img, (tile, tile))
        if commercial:
            cover = ImageEnhance.Brightness(cover).enhance(0.45)
    else:
        cover = Image.new("RGB", (tile, tile), (28, 28, 40))
        cd = ImageDraw.Draw(cover)
        cd.ellipse((48, 48, tile - 48, tile - 48), outline=accent, width=5)
        cd.ellipse((tile // 2 - 12, tile // 2 - 12, tile // 2 + 12, tile // 2 + 12), fill=accent)
        f = _font(26)
        cd.text((tile // 2, tile - 34), "FM", font=f, fill=(235, 235, 245), anchor="mm")
    mask = Image.new("L", (tile, tile), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, tile - 1, tile - 1), 24, fill=255)
    base.paste(cover, (ax, ay), mask)

    # --- text
    d = ImageDraw.Draw(base)
    tx, max_w = 284, 480
    chip_font = _font(15)
    chip_text = station.upper()
    cw = int(d.textlength(chip_text, font=chip_font)) + 22
    d.rounded_rectangle((tx, 38, tx + cw, 62), 12, fill=accent if not commercial else (110, 110, 120))
    d.text((tx + 11, 50), chip_text, font=chip_font, fill=(12, 12, 18), anchor="lm")
    d.text((tx + cw + 12, 50), "● LIVE", font=_font(13), fill=(235, 235, 245, 200), anchor="lm")

    if commercial:
        title, artist = "COMMERCIAL BREAK", note or "Finding music on another station…"
    d.text((tx, 78), _fit_text(d, title, _font(34), max_w), font=_font(34), fill=(255, 255, 255))
    d.text((tx, 126), _fit_text(d, artist, _font(22), max_w), font=_font(22), fill=(205, 205, 220))

    if progress is not None and not commercial:
        d.rounded_rectangle((tx, 170, tx + max_w, 175), 3, fill=(72, 72, 86))
        fill_w = max(6, int(max_w * min(1.0, max(0.0, progress))))
        d.rounded_rectangle((tx, 170, tx + fill_w, 175), 3, fill=accent)

    # --- equalizer frames
    frames_data = _smooth(spectrum[-30:] if len(spectrum) >= 8 else _idle_frames(30))
    if commercial:
        frames_data = [[v * 0.12 for v in f] for f in frames_data]
    bar_w, gap, base_y, max_h = 11, 4, 286, 96
    x0 = tx
    light = tuple(min(255, int(c + (255 - c) * 0.55)) for c in accent)
    caps = [0.0] * BANDS
    frames = []
    for f in frames_data:
        im = base.copy()
        dd = ImageDraw.Draw(im)
        for i, v in enumerate(f):
            h = 5 + v * max_h
            caps[i] = max(caps[i] - 2.2, h)
            x = x0 + i * (bar_w + gap)
            t = min(1.0, v * 1.15)
            col = tuple(int(a + (b - a) * t) for a, b in zip(accent, light)) if not commercial else (95, 95, 105)
            dd.rounded_rectangle((x, base_y - h, x + bar_w, base_y), 4, fill=col)
            if not commercial:
                dd.rounded_rectangle((x, base_y - caps[i] - 6, x + bar_w, base_y - caps[i] - 3), 1, fill=(235, 235, 245))
        frames.append(im.convert("RGB"))

    first = frames[0].quantize(colors=160, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    paletted = [first] + [f.quantize(palette=first, dither=Image.Dither.NONE) for f in frames[1:]]
    buf = io.BytesIO()
    paletted[0].save(buf, "GIF", save_all=True, append_images=paletted[1:], duration=100, loop=0, optimize=False)
    return buf.getvalue(), accent


def looks_like(current: bytes, our_file: str, size: tuple[int, int]) -> bool:
    """True if an image Discord is serving is (nearly) one of our files. Discord re-encodes uploads,
    so compare small greyscale copies rather than bytes."""
    try:
        a = Image.open(io.BytesIO(current)).convert("L").resize(size, Image.LANCZOS)
        b = Image.open(our_file).convert("L").resize(size, Image.LANCZOS)
    except Exception:
        return False
    return float(np.abs(np.asarray(a, dtype=int) - np.asarray(b, dtype=int)).mean()) < 6.0


# ---- public-server station card (static, so it costs almost nothing to draw)
CARD_W, CARD_H = 720, 240
_FREQ = __import__("re").compile(r"(?<!\d)(8[7-9]|9\d|10[0-8])\.(\d)(?!\d)")


def station_accent(name: str) -> tuple[int, int, int]:
    """A stable, vivid colour per station, so each one looks like itself."""
    h = int.from_bytes(__import__("hashlib").sha1(name.lower().encode()).digest()[:2], "big") / 65535
    r, g, b = colorsys.hsv_to_rgb(h, 0.68, 0.95)
    return int(r * 255), int(g * 255), int(b * 255)


def find_frequency(name: str) -> str | None:
    m = _FREQ.search(name or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_w: int, lines: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = w
    out.append(cur)
    if len(out) > lines:
        out = out[:lines]
        out[-1] = _fit_text(draw, out[-1] + " …", font, max_w)
    return [_fit_text(draw, line, font, max_w) for line in out if line]


def render_station_card(name: str, place: str = "", genres: tuple[str, ...] | list[str] = (), quality: str = "", song: bool = False) -> bytes:
    """A 720x240 PNG: a tuning dial on the left, the station's name, place and genres on the right."""
    accent = station_accent(name)
    dim = tuple(int(c * 0.16) for c in accent)
    im = Image.new("RGB", (CARD_W, CARD_H))
    px = ImageDraw.Draw(im)
    for x in range(CARD_W):                    # horizontal gradient from a tinted dark to near black
        t = x / CARD_W
        px.line([(x, 0), (x, CARD_H)], fill=tuple(int(a * (1 - t) + b * t) for a, b in zip((dim[0] + 14, dim[1] + 14, dim[2] + 20), (12, 12, 18))))
    base = im.convert("RGBA")
    d = ImageDraw.Draw(base)

    # --- the dial
    cx, cy, r = 118, 120, 84
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(10, 10, 16), outline=accent, width=4)
    for i in range(0, 41):                                   # tick marks around the upper arc
        ang = math.radians(200 + i * (140 / 40))
        long = i % 5 == 0
        r1, r2 = r - (22 if long else 14), r - 6
        d.line((cx + r1 * math.cos(ang), cy + r1 * math.sin(ang), cx + r2 * math.cos(ang), cy + r2 * math.sin(ang)),
               fill=accent if long else (90, 90, 108), width=3 if long else 2)
    freq = find_frequency(name)
    if freq:                                                  # the needle points at the real dial position
        pos = min(1.0, max(0.0, (float(freq) - 87.5) / 20.5))
    else:
        pos = int.from_bytes(__import__("hashlib").sha1(name.lower().encode()).digest()[2:3], "big") / 255
    ang = math.radians(200 + pos * 140)
    d.line((cx, cy + 18, cx + (r - 14) * math.cos(ang), cy + 18 + (r - 14) * math.sin(ang) + 0), fill=(255, 255, 255), width=3)
    d.ellipse((cx - 7, cy + 11, cx + 7, cy + 25), fill=accent)
    label = "♪" if song else (freq or "FM")
    d.text((cx, cy + 52), label, font=_font(30 if (freq or song) else 26), fill=(240, 240, 248), anchor="mm")
    d.text((cx, cy + 74), "TRACK" if song else ("MHz" if freq else "RADIO"), font=_font(12), fill=(150, 150, 168), anchor="mm")

    # --- text
    tx, max_w = 236, CARD_W - 236 - 28
    chip_font = _font(14)
    chip_w = 108 if song else 78
    d.rounded_rectangle((tx, 28, tx + chip_w, 50), 11, fill=accent)
    d.text((tx + chip_w // 2, 39), "♪ PLAYING" if song else "● LIVE", font=chip_font, fill=(12, 12, 18), anchor="mm")
    if quality:
        d.text((tx + chip_w + 14, 39), quality, font=_font(13), fill=(165, 165, 182), anchor="lm")
    title_font = _font(34)
    y = 68
    for line in _wrap(d, name or "Radio", title_font, max_w, 2):
        d.text((tx, y), line, font=title_font, fill=(255, 255, 255))
        y += 42
    if place:
        d.text((tx, CARD_H - 70), _fit_text(d, place, _font(20), max_w), font=_font(20), fill=(205, 205, 220))
    chip_x = tx
    for g in list(genres)[:3]:
        gw = int(d.textlength(g, font=_font(14))) + 20
        if chip_x + gw > tx + max_w:
            break
        d.rounded_rectangle((chip_x, CARD_H - 42, chip_x + gw, CARD_H - 18), 12, outline=accent, width=2)
        d.text((chip_x + gw / 2, CARD_H - 30), g, font=_font(14), fill=(235, 235, 245), anchor="mm")
        chip_x += gw + 8
    buf = io.BytesIO()
    base.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()
