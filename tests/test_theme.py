import io
import tempfile
import unittest

from PIL import Image

import directory as D
import picker as PK
import premium as PR
import theme as T
from store import Store
from tests.test_publicradio import make_radio


def png(size=(400, 300), color=(200, 30, 30, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


STATION = D.Station(uuid="u1", name="Test FM 101.5", url="http://x", state="IL", city="Chicago", market="Chicago",
                    genres=frozenset({"Rock"}), bitrate=128, codec="MP3")


class Helpers(unittest.TestCase):
    def test_colors(self):
        self.assertEqual(T.parse_color("#ff8800"), 0xFF8800)
        self.assertEqual(T.parse_color("FF8800"), 0xFF8800)
        for bad in ("", None, "red", "#fff", "#gggggg", "ff88001"):
            self.assertIsNone(T.parse_color(bad))

    def test_text_is_single_line_limited_and_cannot_ping(self):
        self.assertEqual(T.clean_text("  Hello\n\tWorld  ", 32), "Hello World")
        self.assertNotIn("@", T.clean_text("@everyone hi", 32))
        self.assertEqual(len(T.clean_text("x" * 500, T.NAME_MAX)), T.NAME_MAX)
        self.assertIsNone(T.clean_text("   ", 10))

    def test_logo_is_validated_and_shrunk(self):
        out = Image.open(io.BytesIO(T.make_logo(png((1000, 600)))))
        self.assertLessEqual(max(out.size), T.LOGO_SIZE)
        with self.assertRaises(ValueError):
            T.make_logo(b"not an image")
        with self.assertRaises(ValueError):
            T.make_logo(b"0" * (T.LOGO_MAX_BYTES + 1))


class Rendering(unittest.IsolatedAsyncioTestCase):
    async def test_theme_changes_title_color_footer_and_card(self):
        theme = T.Theme(1, "Pixel Lounge FM", 0xFF8800, "Always on", png((64, 64)))
        embed, card = await PK.now_playing(STATION, "Now playing", theme=theme)
        self.assertIn("Pixel Lounge FM", embed.title)
        self.assertEqual(embed.color.value, 0xFF8800)
        self.assertEqual(embed.footer.text, "Always on")
        plain, plain_card = await PK.now_playing(STATION, "Now playing")
        self.assertIn("Now playing", plain.title)
        self.assertNotEqual(card.fp.read(), plain_card.fp.read(), "the themed card is drawn differently")

    async def test_garbage_theme_is_ignored(self):
        embed, _ = await PK.now_playing(STATION, "Now playing", theme=object())
        self.assertIn("Now playing", embed.title)

    async def test_idle_panel_uses_the_logo_as_thumbnail(self):
        embed, files = await PK.panel_content(None, theme=T.Theme(1, "Pixel FM", 0x123456, None, png((64, 64))))
        self.assertIn("Pixel FM", embed.title)
        self.assertEqual([f.filename for f in files], ["logo.png"])


class Persistence(unittest.IsolatedAsyncioTestCase):
    async def test_theme_applies_only_while_premium_and_survives_restart(self):
        radio, bot = make_radio([])
        radio.store = Store(tempfile.mkdtemp())
        await radio.store.open()
        bot.premium = PR.Premium(None, sku_id="111")
        await radio.save_theme(T.Theme(1, "Pixel FM", 0xFF8800, "hi", png((64, 64))))
        self.assertIsNone(radio.theme_for(1), "not premium: theme is kept but not shown")
        bot.premium.guilds.add(1)
        self.assertEqual(radio.theme_for(1).name, "Pixel FM")
        radio._themes = await radio.store.all_themes()                 # a restart
        self.assertEqual((radio.theme_for(1).color, radio.theme_for(1).tagline), (0xFF8800, "hi"))
        self.assertTrue(radio.theme_for(1).logo)
        await radio.save_theme(T.Theme(1))                              # reset
        self.assertIsNone(radio.theme_for(1))
        radio._themes = await radio.store.all_themes()
        self.assertEqual(radio._themes, {})
        await radio.store.close()


if __name__ == "__main__":
    unittest.main()
