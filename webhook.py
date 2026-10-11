"""A tiny web server: /healthz for Fly, and /topgg where top.gg delivers votes.

TOPGG_WEBHOOK_SECRET decides how deliveries are checked: a secret starting `whs_` uses top.gg's signed (v1)
webhooks (x-topgg-signature), anything else uses the legacy shared secret in the Authorization header.
With no secret, /topgg refuses everything."""
import hashlib
import hmac
import json
import logging
import os
import time

from aiohttp import web

log = logging.getLogger("webhook")

MAX_BODY = 16_384
TOLERANCE_S = 300
PORT = int(os.getenv("PORT", "8080"))


def verify_v1(secret: str, header: str, body: bytes, now: float | None = None) -> bool:
    """x-topgg-signature is 't=<unix>,v1=<hex>'; the hex is HMAC-SHA256 of '<t>.<raw body>' keyed with the secret."""
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    stamp, sig = parts.get("t", ""), parts.get("v1", "")
    if not stamp.isdigit() or not sig:
        return False
    if abs((now if now is not None else time.time()) - int(stamp)) > TOLERANCE_S:
        return False
    want = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def verify_legacy(secret: str, auth: str) -> bool:
    return hmac.compare_digest((auth or "").encode(), secret.encode())


def parse_vote(payload) -> tuple[str, int | None, str | None] | None:
    """('vote', discord user id, vote id) for a real vote, ('test', None, None) for a dashboard test, else None."""
    if not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    try:
        if kind == "vote.create":                       # v1
            data = payload["data"]
            return "vote", int(data["user"]["platform_id"]), str(data.get("id") or "") or None
        if kind in ("webhook.test", "test"):
            return "test", None, None
        if kind == "upvote":                            # legacy v0
            return "vote", int(payload["user"]), None
    except (KeyError, TypeError, ValueError):
        return None
    return None


def make_app(votes) -> web.Application:
    async def health(request: web.Request):
        return web.Response(text="ok")

    async def topgg(request: web.Request):
        secret = votes.secret
        if not secret:
            return web.Response(status=404)
        body = await request.content.read(MAX_BODY + 1)
        if len(body) > MAX_BODY:
            return web.Response(status=413)
        if secret.startswith("whs_"):
            ok = verify_v1(secret, request.headers.get("x-topgg-signature", ""), body)
        else:
            ok = verify_legacy(secret, request.headers.get("Authorization", ""))
        if not ok:
            log.warning("Rejected a vote delivery with a bad signature")
            return web.Response(status=401)
        try:
            parsed = parse_vote(json.loads(body))
        except ValueError:
            return web.Response(status=400)
        if parsed is None:
            return web.Response(status=400)
        kind, user_id, vote_id = parsed
        if kind == "vote":
            await votes.record_vote(user_id, vote_id)
        else:
            log.info("top.gg test delivery received")
        return web.json_response({"ok": True})

    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/healthz", health)
    app.router.add_post("/topgg", topgg)
    return app


async def start(votes, port: int = PORT) -> web.AppRunner:
    runner = web.AppRunner(make_app(votes))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Web server listening on port %d (vote webhook %s)", port, "on" if votes.enabled else "off")
    return runner
