"""Discord premium (server subscription) checks.

Set PREMIUM_SKU_ID to the SKU of your server subscription (Developer Portal > Monetization). Without
it, premium is off and nothing changes for anyone. Entitlements are loaded at startup, kept current
by Discord's entitlement events, and re-synced now and then in case an event is missed."""
import logging
import os

import discord

log = logging.getLogger("premium")

SKU_ID = os.getenv("PREMIUM_SKU_ID", "").strip()
# Extra simultaneous radio servers that only premium servers can use (on top of MAX_PUBLIC_STREAMS),
# so a premium server can still start when the free slots are full.
PREMIUM_RESERVE = int(os.getenv("PREMIUM_RESERVED_STREAMS", "2"))


class Premium:
    def __init__(self, bot, sku_id: str | None = None):
        self.bot = bot
        self.sku_id = SKU_ID if sku_id is None else sku_id
        self.guilds: set[int] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.sku_id)

    def is_premium(self, guild_id: int | None) -> bool:
        """True when this server has an active premium subscription."""
        return self.enabled and guild_id in self.guilds

    def has_priority(self, guild_id: int | None) -> bool:
        return self.is_premium(guild_id)

    def extra_slots(self, guild_id: int | None) -> int:
        return PREMIUM_RESERVE if self.is_premium(guild_id) else 0

    async def refresh(self):
        """Reload every active server subscription from Discord."""
        if not self.enabled:
            return
        found: set[int] = set()
        try:
            async for ent in self.bot.entitlements(skus=[discord.Object(int(self.sku_id))], exclude_ended=True):
                if ent.guild_id and ent.is_active():
                    found.add(ent.guild_id)
        except (discord.HTTPException, ValueError) as e:
            log.warning("Couldn't load premium subscriptions (keeping the last known list): %s", e)
            return
        self.guilds = found
        log.info("Premium servers: %d", len(found))

    def on_entitlement(self, ent) -> None:
        """Apply a create/update/delete event right away."""
        if not self.enabled or not ent.guild_id or str(ent.sku_id) != self.sku_id:
            return
        if ent.is_active():
            self.guilds.add(ent.guild_id)
        else:
            self.guilds.discard(ent.guild_id)
