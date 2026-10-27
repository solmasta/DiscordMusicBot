import os
import sqlite3
import asyncio
import logging
import discord
from discord.ext import commands
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pollbot")

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
POLL_CHANNEL_ID = int(os.getenv("POLL_CHANNEL_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "data/votes.db")

# ── database ──────────────────────────────────────────────────────────────────

def init_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            user_id INTEGER PRIMARY KEY,
            faction TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS poll_meta (
            id      INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL
        )
    """)
    con.commit()
    return con

db = init_db()

def get_counts():
    cur = db.execute("SELECT faction, COUNT(*) FROM votes GROUP BY faction")
    rows = dict(cur.fetchall())
    return rows.get("Alliance", 0), rows.get("Horde", 0)

def upsert_vote(user_id: int, faction: str) -> str | None:
    """Returns 'same' if unchanged, old faction str if switched, None if new."""
    cur = db.execute("SELECT faction FROM votes WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    old = row[0] if row else None
    if old == faction:
        return "same"
    db.execute(
        "INSERT OR REPLACE INTO votes (user_id, faction) VALUES (?, ?)",
        (user_id, faction),
    )
    db.commit()
    return old

def save_poll_meta(message_id: int, channel_id: int):
    db.execute(
        "INSERT OR REPLACE INTO poll_meta (id, message_id, channel_id) VALUES (1, ?, ?)",
        (message_id, channel_id),
    )
    db.commit()

def load_poll_meta():
    cur = db.execute("SELECT message_id, channel_id FROM poll_meta WHERE id = 1")
    return cur.fetchone()

# ── embed ─────────────────────────────────────────────────────────────────────

def build_embed(alliance: int, horde: int) -> discord.Embed:
    total = alliance + horde

    def bar(n):
        if total == 0:
            return "░" * 20
        return "█" * round(n / total * 20) + "░" * (20 - round(n / total * 20))

    def pct(n):
        return f"{n/total*100:.1f}%" if total else "—"

    embed = discord.Embed(
        title="⚔️  Alliance or Horde?",
        description=(
            "The eternal question — vote below.\n"
            "You can switch sides anytime. The poll runs **forever**."
        ),
        color=0x0066CC if alliance >= horde else 0xCC0000,
    )
    embed.add_field(
        name="🛡️  Alliance",
        value=f"`{bar(alliance)}` **{alliance}** votes ({pct(alliance)})",
        inline=False,
    )
    embed.add_field(
        name="🔴  Horde",
        value=f"`{bar(horde)}` **{horde}** votes ({pct(horde)})",
        inline=False,
    )
    if total:
        leader = "Alliance 🛡️" if alliance > horde else ("Horde 🔴" if horde > alliance else "Tied ⚖️")
        embed.set_footer(text=f"Total votes: {total}  •  Leading: {leader}")
    else:
        embed.set_footer(text="No votes yet — be the first!")
    return embed

# ── persistent view ───────────────────────────────────────────────────────────

class PollView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _vote(self, interaction: discord.Interaction, faction: str):
        result = upsert_vote(interaction.user.id, faction)
        if result == "same":
            await interaction.response.send_message(
                f"You're already on **{faction}**!", ephemeral=True
            )
            return
        alliance, horde = get_counts()
        await interaction.message.edit(embed=build_embed(alliance, horde), view=self)
        if result is None:
            reply = f"You voted **{faction}**! ✅"
        else:
            reply = f"Switched from **{result}** → **{faction}**! ✅"
        await interaction.response.send_message(reply, ephemeral=True)
        log.info("Vote: %s → %s (A=%d H=%d)", interaction.user, faction, alliance, horde)

    @discord.ui.button(
        label="Alliance", emoji="🛡️",
        style=discord.ButtonStyle.primary,
        custom_id="poll:alliance",
    )
    async def btn_alliance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "Alliance")

    @discord.ui.button(
        label="Horde", emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="poll:horde",
    )
    async def btn_horde(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "Horde")

# ── bot ───────────────────────────────────────────────────────────────────────

class PollBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.add_view(PollView())  # re-attach persistent view on restart

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="Alliance vs Horde 🗳️",
            )
        )
        await self._ensure_poll()

    async def _ensure_poll(self):
        channel = self.get_channel(POLL_CHANNEL_ID)
        if not channel:
            log.warning("POLL_CHANNEL_ID %s not found", POLL_CHANNEL_ID)
            return

        row = load_poll_meta()
        if row:
            try:
                msg = await channel.fetch_message(row[0])
                alliance, horde = get_counts()
                await msg.edit(embed=build_embed(alliance, horde), view=PollView())
                log.info("Poll refreshed (msg=%s)", row[0])
                return
            except discord.NotFound:
                log.warning("Saved poll message missing — reposting")

        alliance, horde = get_counts()
        msg = await channel.send(embed=build_embed(alliance, horde), view=PollView())
        save_poll_meta(msg.id, channel.id)
        try:
            await msg.pin()
            log.info("Poll pinned (msg=%s)", msg.id)
        except discord.Forbidden:
            log.warning("Missing Manage Messages permission — poll not pinned")
        except Exception as e:
            log.warning("Could not pin poll: %s", e)
        log.info("Poll posted (msg=%s)", msg.id)

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        msg = f"Error: `{error}`"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception:
            pass

# ── health server ─────────────────────────────────────────────────────────────

async def health_server():
    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="OK"))
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server on port %d", port)

# ── entry ─────────────────────────────────────────────────────────────────────

async def main():
    if not TOKEN:
        raise ValueError("DISCORD_TOKEN is not set")
    bot = PollBot()
    async with bot:
        await asyncio.gather(bot.start(TOKEN), health_server())

if __name__ == "__main__":
    asyncio.run(main())
