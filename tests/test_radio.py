import unittest

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from cogs.radio import fetch_now_playing, resolve_stream


def make_app():
    async def stream(request):
        meta = b"StreamTitle='Artist - Song';"
        meta += b"\0" * (-len(meta) % 16)
        resp = web.StreamResponse(headers={
            "Content-Type": "audio/mpeg", "icy-name": "Test FM", "icy-metaint": "8"})
        await resp.prepare(request)
        await resp.write(b"x" * 8 + bytes([len(meta) // 16]) + meta + b"y" * 8)
        await resp.write_eof()
        return resp

    async def pls(request):
        base = f"{request.scheme}://{request.host}"
        return web.Response(text=f"[playlist]\nFile1={base}/stream\n")

    async def page(request):
        return web.Response(text="<html></html>", content_type="text/html")

    async def missing(request):
        return web.Response(status=404)

    app = web.Application()
    app.add_routes([web.get("/stream", stream), web.get("/s.pls", pls),
                    web.get("/page", page), web.get("/gone", missing)])
    return app


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = TestServer(make_app())
        await self.server.start_server()
        self.session = aiohttp.ClientSession()
        self.base = f"http://{self.server.host}:{self.server.port}"

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()

    async def test_direct_stream_returns_station_name(self):
        url, name = await resolve_stream(self.session, f"{self.base}/stream")
        self.assertEqual((url, name), (f"{self.base}/stream", "Test FM"))

    async def test_pls_playlist_is_unwrapped(self):
        url, name = await resolve_stream(self.session, f"{self.base}/s.pls")
        self.assertEqual(url, f"{self.base}/stream")
        self.assertEqual(name, "Test FM")

    async def test_rejects_non_http_html_and_errors(self):
        for bad in ("ftp://example.com/x", f"{self.base}/page", f"{self.base}/gone"):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    await resolve_stream(self.session, bad)

    async def test_icy_song_title(self):
        self.assertEqual(await fetch_now_playing(self.session, f"{self.base}/stream"), "Artist - Song")
