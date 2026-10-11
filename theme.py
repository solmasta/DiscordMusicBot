"""Premium server themes: a server's own name, colour, tagline and logo on the radio card and panel."""
import io
import re
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

NAME_MAX, TAGLINE_MAX = 32, 80
LOGO_MAX_BYTES = 2_000_000
LOGO_SIZE = 256

PRESETS = {
    "Crüe Red": 0xE63946, "Midnight": 0x4361EE, "Sunset": 0xFF7B00, "Neon Green": 0x2BD96B,
    "Violet": 0x8B5CF6, "Gold": 0xF4B400, "Ocean": 0x00A8CC, "Mono": 0xB4B4C0,
}
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


@dataclass
class Theme:
    guild_id: int
    name: str | None = None
    color: int | None = None         # 0xRRGGBB
    tagline: str | None = None
    logo: bytes | None = None        # a small PNG

    @property
    def accent(self) -> tuple[int, int, int] | None:
        if self.color is None:
            return None
        return (self.color >> 16) & 255, (self.color >> 8) & 255, self.color & 255

    @property
    def is_empty(self) -> bool:
        return not (self.name or self.color is not None or self.tagline or self.logo)


def parse_color(text: str | None) -> int | None:
    """'#ff8800' or 'ff8800' -> int; None when it isn't a 6-digit hex colour."""
    m = _HEX.match((text or "").strip())
    return int(m.group(1), 16) if m else None


def clean_text(text: str | None, limit: int) -> str | None:
    """Single line, no control characters, no @mentions; None when empty."""
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip().replace("@", "＠")
    return text[:limit] or None


def make_logo(data: bytes) -> bytes:
    """Validate an uploaded image and return a small square-ish PNG. Raises ValueError with a friendly reason."""
    if len(data) > LOGO_MAX_BYTES:
        raise ValueError("That image is too big. Please use one under 2 MB.")
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("I couldn't read that as an image. Use a PNG, JPG or WebP.")
    if im.width * im.height > 25_000_000:
        raise ValueError("That image is too large. Please use a smaller one.")
    im = im.convert("RGBA")
    im.thumbnail((LOGO_SIZE, LOGO_SIZE))
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()
