"""Vote perks: a top.gg vote earns one credit; spending it with /vote gives that server a 12-hour boost
(true 24/7 and priority slots). Credits and boosts are saved so a restart or deploy doesn't lose them."""
import logging
import os
import time

log = logging.getLogger("votes")

WEBHOOK_SECRET = os.getenv("TOPGG_WEBHOOK_SECRET", "").strip()
BOOST_S = 12 * 3600          # each claimed vote adds this much boost
MAX_AHEAD_S = 48 * 3600      # a server's boost never runs more than this far into the future
MAX_CREDITS = 4              # unclaimed votes a person can bank
LEGACY_GAP_S = 6 * 3600      # the old top.gg webhook has no vote id: treat a repeat inside this gap as a retry
SEEN_KEEP_S = 3 * 86400


class VoteBoosts:
    def __init__(self, bot, secret: str | None = None, clock=time.time):
        self.bot = bot
        self.secret = WEBHOOK_SECRET if secret is None else secret
        self.clock = clock
        self.credits: dict[int, int] = {}
        self.last_vote: dict[int, float] = {}
        self.boosts: dict[int, float] = {}      # server -> epoch seconds the boost ends
        self.seen: dict[str, float] = {}        # vote ids already counted (top.gg retries deliveries)

    @property
    def enabled(self) -> bool:
        return bool(self.secret)

    @property
    def store(self):
        return self.bot.public.store

    async def load(self):
        try:
            self.credits, self.last_vote = await self.store.all_credits()
            self.boosts = await self.store.all_boosts()
            self.seen = await self.store.recent_votes(self.clock() - SEEN_KEEP_S)
        except Exception as e:
            log.error("Saved votes are unavailable (%s); voting still works but won't be remembered", e)

    # ---- state
    def is_boosted(self, guild_id: int | None) -> bool:
        return self.boosts.get(guild_id, 0) > self.clock()

    def boost_left(self, guild_id: int | None) -> float:
        return max(0.0, self.boosts.get(guild_id, 0) - self.clock())

    def credits_for(self, user_id: int) -> int:
        return self.credits.get(user_id, 0)

    # ---- events
    async def record_vote(self, user_id: int, vote_id: str | None = None) -> bool:
        """Count a vote. False when it's a repeat delivery of one we already counted."""
        now = self.clock()
        if vote_id:
            if vote_id in self.seen:
                return False
            self.seen[vote_id] = now
        elif now - self.last_vote.get(user_id, 0) < LEGACY_GAP_S:
            return False
        self.credits[user_id] = min(MAX_CREDITS, self.credits.get(user_id, 0) + 1)
        self.last_vote[user_id] = now
        try:
            await self.store.save_credit(user_id, self.credits[user_id], now)
            if vote_id:
                await self.store.save_vote_seen(vote_id, now)
        except Exception as e:
            log.warning("Could not save a vote: %s", e)
        log.info("Vote counted for user %s (now %d credit(s))", user_id, self.credits[user_id])
        return True

    async def claim(self, user_id: int, guild_id: int) -> tuple[bool, float]:
        """Spend one credit on a server. Returns (spent, seconds of boost the server now has)."""
        if self.credits.get(user_id, 0) <= 0:
            return False, self.boost_left(guild_id)
        now = self.clock()
        self.credits[user_id] -= 1
        until = min(max(now, self.boosts.get(guild_id, 0)) + BOOST_S, now + MAX_AHEAD_S)
        self.boosts[guild_id] = until
        try:
            await self.store.save_credit(user_id, self.credits[user_id], self.last_vote.get(user_id, now))
            await self.store.save_boost(guild_id, until)
        except Exception as e:
            log.warning("Could not save a boost: %s", e)
        return True, until - now
