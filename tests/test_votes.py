import hashlib
import hmac
import json
import tempfile
import unittest
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

import publicradio as P
import premium as PR
import votes as V
import webhook as W
from store import Store
from tests.test_publicradio import make_radio


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


async def make_votes(secret="s3cret"):
    radio, bot = make_radio([])
    radio.store = Store(tempfile.mkdtemp())
    await radio.store.open()
    bot.public = radio
    clock = Clock()
    votes = V.VoteBoosts(bot, secret=secret, clock=clock)
    bot.votes = votes
    return votes, radio, bot, clock


class Credits(unittest.IsolatedAsyncioTestCase):
    async def test_vote_claim_stack_cap_and_expiry(self):
        votes, radio, bot, clock = await make_votes()
        self.assertTrue(await votes.record_vote(7, "v1"))
        self.assertFalse(await votes.record_vote(7, "v1"), "a retried delivery is not a second vote")
        ok, left = await votes.claim(7, 100)
        self.assertTrue(ok)
        self.assertEqual(left, V.BOOST_S)
        self.assertTrue(votes.is_boosted(100))
        self.assertFalse((await votes.claim(7, 100))[0], "no credits left")
        for i in range(10):
            await votes.record_vote(7, f"x{i}")
        self.assertEqual(votes.credits_for(7), V.MAX_CREDITS, "banked votes are capped")
        for _ in range(4):
            await votes.claim(7, 100)
        self.assertLessEqual(votes.boost_left(100), V.MAX_AHEAD_S, "a boost can't run further ahead than the cap")
        clock.t += V.MAX_AHEAD_S + 1
        self.assertFalse(votes.is_boosted(100))
        await radio.store.close()

    async def test_legacy_votes_without_an_id_dedupe_by_time(self):
        votes, radio, bot, clock = await make_votes()
        self.assertTrue(await votes.record_vote(7))
        self.assertFalse(await votes.record_vote(7))
        clock.t += V.LEGACY_GAP_S + 1
        self.assertTrue(await votes.record_vote(7))
        await radio.store.close()

    async def test_survives_a_restart(self):
        votes, radio, bot, clock = await make_votes()
        await votes.record_vote(7, "v1")
        await votes.record_vote(8, "v2")
        await votes.claim(7, 100)
        fresh = V.VoteBoosts(bot, secret="s3cret", clock=clock)
        await fresh.load()
        self.assertEqual((fresh.credits_for(7), fresh.credits_for(8)), (0, 1))
        self.assertTrue(fresh.is_boosted(100))
        self.assertFalse(await fresh.record_vote(7, "v1"), "the same vote is still recognised after a restart")
        await radio.store.close()


class Perks(unittest.IsolatedAsyncioTestCase):
    async def test_boost_gives_slots_and_24_7_but_not_themes(self):
        votes, radio, bot, clock = await make_votes()
        bot.premium = PR.Premium(None, sku_id="111")
        with mock.patch.object(P, "MAX_STREAMS", 1):
            self.assertEqual(radio.slot_limit(100), 1)
            await votes.record_vote(7, "v1")
            await votes.claim(7, 100)
            self.assertEqual(radio.slot_limit(100), 1 + PR.PREMIUM_RESERVE)
        self.assertTrue(radio.has_perks(100))
        self.assertFalse(radio.is_premium(100), "a vote is not a subscription: themes and favorites stay premium")
        self.assertFalse(radio.has_perks(101))
        await radio.store.close()


def sign(secret, body, t):
    return f"t={t},v1=" + hmac.new(secret.encode(), str(t).encode() + b"." + body, hashlib.sha256).hexdigest()


class Webhook(unittest.IsolatedAsyncioTestCase):
    def test_signature_check(self):
        body, now = b'{"a":1}', 1_700_000_000
        good = sign("whs_x", body, now)
        self.assertTrue(W.verify_v1("whs_x", good, body, now))
        self.assertFalse(W.verify_v1("whs_x", good, b'{"a":2}', now), "body changed")
        self.assertFalse(W.verify_v1("whs_y", good, body, now), "wrong secret")
        self.assertFalse(W.verify_v1("whs_x", good, body, now + 1000), "too old (replay)")
        for bad in ("", "t=abc,v1=zz", "garbage"):
            self.assertFalse(W.verify_v1("whs_x", bad, body, now))
        self.assertTrue(W.verify_legacy("pw", "pw"))
        self.assertFalse(W.verify_legacy("pw", "nope"))
        self.assertFalse(W.verify_legacy("pw", ""))

    def test_parse(self):
        v1 = {"type": "vote.create", "data": {"id": "55", "user": {"id": "top", "platform_id": "123"}}}
        self.assertEqual(W.parse_vote(v1), ("vote", 123, "55"))
        self.assertEqual(W.parse_vote({"type": "upvote", "user": "123"}), ("vote", 123, None))
        self.assertEqual(W.parse_vote({"type": "webhook.test", "data": {}})[0], "test")
        for bad in (None, [], {"type": "vote.create"}, {"type": "upvote", "user": "x"}, {"type": "other"}):
            self.assertIsNone(W.parse_vote(bad))

    async def test_endpoint(self):
        import time
        votes, radio, bot, clock = await make_votes(secret="whs_secret")
        async with TestClient(TestServer(W.make_app(votes))) as c:
            body = json.dumps({"type": "vote.create", "data": {"id": "9", "user": {"platform_id": "42"}}}).encode()
            r = await c.post("/topgg", data=body, headers={"x-topgg-signature": sign("whs_secret", body, int(time.time()))})
            self.assertEqual(r.status, 200)
            self.assertEqual(votes.credits_for(42), 1)
            r = await c.post("/topgg", data=body, headers={"x-topgg-signature": sign("whs_secret", body, int(time.time()))})
            self.assertEqual((r.status, votes.credits_for(42)), (200, 1), "a retry is acknowledged but not counted twice")
            r = await c.post("/topgg", data=body, headers={"x-topgg-signature": "t=1,v1=bad"})
            self.assertEqual(r.status, 401)
            r = await c.post("/topgg", data=body)
            self.assertEqual(r.status, 401)
            r = await c.post("/topgg", data=b"x" * 20000, headers={"x-topgg-signature": "t=1,v1=bad"})
            self.assertEqual(r.status, 413)
            self.assertEqual((await c.get("/healthz")).status, 200)
        votes.secret = ""
        async with TestClient(TestServer(W.make_app(votes))) as c:
            self.assertEqual((await c.post("/topgg", data=b"{}")).status, 404, "off without a secret")
        await radio.store.close()

    async def test_legacy_secret(self):
        votes, radio, bot, clock = await make_votes(secret="pw")
        async with TestClient(TestServer(W.make_app(votes))) as c:
            body = json.dumps({"type": "upvote", "user": "5"}).encode()
            self.assertEqual((await c.post("/topgg", data=body, headers={"Authorization": "pw"})).status, 200)
            self.assertEqual(votes.credits_for(5), 1)
            self.assertEqual((await c.post("/topgg", data=body, headers={"Authorization": "wrong"})).status, 401)
        await radio.store.close()


if __name__ == "__main__":
    unittest.main()
