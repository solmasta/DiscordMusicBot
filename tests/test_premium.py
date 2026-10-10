import types
import unittest
from unittest import mock

import premium as PR
import publicradio as P
from tests.test_publicradio import make_radio


def ent(guild_id, sku="111", active=True):
    return types.SimpleNamespace(guild_id=guild_id, sku_id=int(sku), is_active=lambda: active)


class PremiumState(unittest.TestCase):
    def test_disabled_without_sku(self):
        p = PR.Premium(None, sku_id="")
        p.on_entitlement(ent(5))
        self.assertFalse(p.enabled)
        self.assertFalse(p.is_premium(5))
        self.assertEqual(p.extra_slots(5), 0)

    def test_events_add_and_remove(self):
        p = PR.Premium(None, sku_id="111")
        p.on_entitlement(ent(5))
        self.assertTrue(p.is_premium(5))
        self.assertEqual(p.extra_slots(5), PR.PREMIUM_RESERVE)
        p.on_entitlement(ent(5, active=False))
        self.assertFalse(p.is_premium(5))

    def test_other_sku_and_user_entitlements_ignored(self):
        p = PR.Premium(None, sku_id="111")
        p.on_entitlement(ent(5, sku="222"))
        p.on_entitlement(ent(None))
        self.assertFalse(p.is_premium(5))
        self.assertFalse(p.is_premium(None))


class PremiumSlots(unittest.IsolatedAsyncioTestCase):
    async def test_slot_limit(self):
        radio, bot = make_radio([])
        bot.premium = PR.Premium(None, sku_id="111")
        bot.premium.guilds.add(7)
        with mock.patch.object(P, "MAX_STREAMS", 1):
            self.assertEqual(radio.slot_limit(1), 1)
            self.assertEqual(radio.slot_limit(7), 1 + PR.PREMIUM_RESERVE)


if __name__ == "__main__":
    unittest.main()
