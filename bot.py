import os
import re
import sqlite3
import datetime
from typing import Optional
from dotenv import load_dotenv
from aiohttp import web

import discord
from discord.ext import commands
from discord import app_commands

load_dotenv()
TOKEN = os.getenv("TOKEN")

env_whitelist = os.getenv("WHITELIST", "")
WHITELISTED_USERS = set()
if env_whitelist:
    for uid in env_whitelist.split(","):
        uid = uid.strip()
        if uid.isdigit():
            WHITELISTED_USERS.add(int(uid))

BANNED_WORDS = []

PROFANITY_REGEX = re.compile(
    r"\b(fuck|shit|bitch|asshole|cunt|dick|bastard|pussy|whore|nigger|nigga|faggot|fag|slut)\b", 
    re.IGNORECASE
)

INVITE_REGEX = re.compile(
    r"(?:https?:\/\/)?(?:www\.)?(?:discord\.(?:gg|io|me|li)|discord(?:app)?\.com\/invite)\/[a-zA-Z0-9]+", 
    re.IGNORECASE
)

DB_PATH = "modbot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            user_id INTEGER,
            mod_id INTEGER,
            mod_name TEXT,
            reason TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

def db_add_warning(guild_id: int, user_id: int, mod_id: int, mod_name: str, reason: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cursor.execute("""
        INSERT INTO warnings (guild_id, user_id, mod_id, mod_name, reason, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (guild_id, user_id, mod_id, mod_name, reason, now))
    conn.commit()
    cursor.execute("SELECT COUNT(*) FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    count = cursor.fetchone()[0]
    conn.close()
    return count

def db_get_warnings(guild_id: int, user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, mod_name, reason, timestamp 
        FROM warnings 
        WHERE guild_id = ? AND user_id = ?
        ORDER BY id ASC
    """, (guild_id, user_id))
    rows = cursor.fetchall()
    conn.close()
    return rows

def db_clear_warnings(guild_id: int, user_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted

snipe_cache = {}

intents = discord.Intents.all()
bot = commands.Bot(
    command_prefix=["!", "?"], 
    intents=intents, 
    help_command=None
)

def build_purple_embed(title: Optional[str] = None, description: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.from_rgb(88, 4, 116)
    )
    if os.path.exists("footer.png"):
        embed.set_image(url="attachment://footer.png")
    return embed

def get_footer_file() -> Optional[discord.File]:
    if os.path.exists("footer.png"):
        return discord.File("footer.png", filename="footer.png")
    return None

def is_whitelisted():
    async def predicate(ctx: commands.Context):
        if ctx.author.id in WHITELISTED_USERS:
            return True
        if ctx.guild and (ctx.author.id == ctx.guild.owner_id or ctx.author.guild_permissions.administrator):
            return True
        raise commands.CheckFailure("Unauthorized execution.")
    return commands.check(predicate)

async def handle_ping(request):
    return web.Response(text="Bot is operational.")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[SYSTEM] Web server active on port {port}", flush=True)

@bot.event
async def setup_hook():
    bot.loop.create_task(start_web_server())

@bot.event
async def on_ready():
    print(f"[SYSTEM] Logged in as {bot.user} (ID: {bot.user.id})", flush=True)
    try:
        synced = await bot.tree.sync()
        print(f"[SYSTEM] Commands synced: {len(synced)}", flush=True)
    except Exception as exc:
        print(f"[ERROR] Sync error: {exc}", flush=True)

async def safe_respond(ctx: commands.Context, message: str, ephemeral: bool = False, title: Optional[str] = None):
    embed = build_purple_embed(title=title, description=message)
    f = get_footer_file()
    try:
        if ctx.interaction:
            if ctx.interaction.response.is_done():
                if f:
                    await ctx.interaction.followup.send(embed=embed, file=f, ephemeral=ephemeral)
                else:
                    await ctx.interaction.followup.send(embed=embed, ephemeral=ephemeral)
            else:
                if f:
                    await ctx.interaction.response.send_message(embed=embed, file=f, ephemeral=ephemeral)
                else:
                    await ctx.interaction.response.send_message(embed=embed, ephemeral=ephemeral)
        else:
            if f:
                await ctx.send(embed=embed, file=f)
            else:
                await ctx.send(embed=embed)
    except (discord.NotFound, discord.HTTPException):
        pass

def can_moderate(ctx: commands.Context, member: discord.Member) -> tuple[bool, str]:
    if member == ctx.guild.owner:
        return False, "**Action Denied:** Target is the server owner."
    if member == ctx.author:
        return False, "**Action Denied:** Target cannot be yourself."
    if member.top_role >= ctx.guild.me.top_role:
        return False, "**Hierarchy Error:** My highest role must be placed higher than the target's role."
    if ctx.author.id not in WHITELISTED_USERS and ctx.author.id != ctx.guild.owner_id:
        if member.top_role >= ctx.author.top_role:
            return False, "**Action Denied:** Target role is equal or higher than your own."
    return True, ""

@bot.event
async def on_message_delete(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    snipe_cache[message.channel.id] = {
        "author": f"{message.author} ({message.author.id})",
        "content": message.content or "[Empty / Attachment]",
        "time": datetime.datetime.now(datetime.timezone.utc)
    }

@bot.event
async def on_message(message: discord.Message):
    if not message.guild or message.author.bot:
        return

    is_exempt = (
        message.author.id in WHITELISTED_USERS or
        message.author.id == message.guild.owner_id or
        message.author.guild_permissions.administrator
    )

    if not is_exempt:
        if PROFANITY_REGEX.search(message.content):
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            count = db_add_warning(
                message.guild.id, 
                message.author.id, 
                bot.user.id, 
                "Automod", 
                "Automated Warning: Inappropriate Language / Profanity"
            )
            embed = build_purple_embed(
                title="Automod: Warning Issued",
                description=f"{message.author.mention} has received an automatic warning for profanity.\n**Total Infractions:** `{count}`"
            )
            warning_msg = await message.channel.send(embed=embed)
            await warning_msg.delete(delay=5)
            return

        if len(message.mentions) >= 5:
            try:
                await message.delete()
                await message.author.timeout(datetime.timedelta(minutes=10), reason="Automod: Mass mentions")
                embed = build_purple_embed(
                    title="Automod Enforcement",
                    description=f"{message.author.mention} has been timed out for 10 minutes (Mass mentions)."
                )
                await message.channel.send(embed=embed)
            except discord.Forbidden:
                pass
            return

        if INVITE_REGEX.search(message.content):
            try:
                await message.delete()
                warning = await message.channel.send(f"{message.author.mention}, invite links are not permitted.")
                await warning.delete(delay=3)
            except discord.Forbidden:
                pass
            return

        lowered = message.content.lower()
        if any(bad_word in lowered for bad_word in BANNED_WORDS):
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            return

    if bot.user in message.mentions and not message.mention_everyone:
        embed = build_purple_embed(
            title="Amethyst Moderation",
            description=f"Hello {message.author.mention}. Use `/modhelp` or `!modhelp` to see active commands."
        )
        f = get_footer_file()
        if f:
            await message.reply(embed=embed, file=f)
        else:
            await message.reply(embed=embed)
        return

    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandInvokeError):
        error = error.original

    if isinstance(error, commands.CheckFailure):
        await safe_respond(ctx, "**Permission Denied:** You are not authorized to invoke this command.", ephemeral=True)
    elif isinstance(error, discord.Forbidden):
        await safe_respond(ctx, "**Execution Failed:** Missing permissions or hierarchy block.", ephemeral=True)
    elif isinstance(error, commands.MemberNotFound):
        await safe_respond(ctx, "**Lookup Error:** Member not found.", ephemeral=True)
    elif isinstance(error, commands.MissingRequiredArgument):
        await safe_respond(ctx, f"**Syntax Error:** Parameter *{error.param.name}* required.", ephemeral=True)
    else:
        await safe_respond(ctx, f"**Error:** `{error}`", ephemeral=True)

@bot.hybrid_command(name="say", description="Broadcasts a message.")
@is_whitelisted()
@app_commands.describe(message="Text to send", channel="Target channel")
async def say(ctx: commands.Context, message: str, channel: Optional[discord.TextChannel] = None):
    target = channel or ctx.channel
    if not ctx.interaction and ctx.message:
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass
            
    await target.send(message)
    if ctx.interaction:
        await safe_respond(ctx, "Command executed.", ephemeral=True)

@bot.hybrid_command(name="kick", description="Kicks a member.")
@is_whitelisted()
@app_commands.describe(member="Member to kick", reason="Reason")
async def kick(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = "None provided"):
    allowed, msg = can_moderate(ctx, member)
    if not allowed:
        return await safe_respond(ctx, msg, ephemeral=True)
        
    await member.kick(reason=reason)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}\n**Reason:** *{reason}*",
        title="Action: Kick"
    )

@bot.hybrid_command(name="ban", description="Bans a member.")
@is_whitelisted()
@app_commands.describe(member="Member to ban", delete_days="Message history purge (0-7)", reason="Reason")
async def ban(ctx: commands.Context, member: discord.Member, delete_days: Optional[int] = 0, *, reason: Optional[str] = "None provided"):
    allowed, msg = can_moderate(ctx, member)
    if not allowed:
        return await safe_respond(ctx, msg, ephemeral=True)
        
    secs = max(0, min(delete_days, 7)) * 86400
    await member.ban(reason=reason, delete_message_seconds=secs)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}\n**Reason:** *{reason}*",
        title="Action: Ban"
    )

@bot.hybrid_command(name="unban", description="Unbans user by ID.")
@is_whitelisted()
@app_commands.describe(user_id="User ID", reason="Reason")
async def unban(ctx: commands.Context, user_id: str, *, reason: Optional[str] = "None provided"):
    try:
        user = await bot.fetch_user(int(user_id))
        await ctx.guild.unban(user, reason=reason)
        await safe_respond(
            ctx, 
            f"**Target:** {user.name} (`{user.id}`)\n**Moderator:** {ctx.author.mention}\n**Reason:** *{reason}*",
            title="Action: Unban"
        )
    except discord.NotFound:
        await safe_respond(ctx, "**Lookup Error:** No ban record found.", ephemeral=True)
    except ValueError:
        await safe_respond(ctx, "**Syntax Error:** ID must be numeric.", ephemeral=True)

@bot.hybrid_command(name="softban", description="Bans and instantly unbans to clear past messages.")
@is_whitelisted()
@app_commands.describe(member="Target member", reason="Reason")
async def softban(ctx: commands.Context, member: discord.Member, *, reason: Optional[str] = "None provided"):
    allowed, msg = can_moderate(ctx, member)
    if not allowed:
        return await safe_respond(ctx, msg, ephemeral=True)
        
    await member.ban(reason=f"Softban: {reason}", delete_message_seconds=604800)
    await ctx.guild.unban(member, reason="Softban cleanup")
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}\n**Reason:** *{reason}*",
        title="Action: Softban"
    )

@bot.hybrid_command(name="timeout", description="Times out a member.")
@is_whitelisted()
@app_commands.describe(member="Member", minutes="Minutes", reason="Reason")
async def timeout(ctx: commands.Context, member: discord.Member, minutes: int, *, reason: Optional[str] = "None provided"):
    allowed, msg = can_moderate(ctx, member)
    if not allowed:
        return await safe_respond(ctx, msg, ephemeral=True)
        
    await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Duration:** `{minutes}m`\n**Moderator:** {ctx.author.mention}\n**Reason:** *{reason}*",
        title="Action: Timeout"
    )

@bot.hybrid_command(name="untimeout", description="Removes timeout.")
@is_whitelisted()
@app_commands.describe(member="Member")
async def untimeout(ctx: commands.Context, member: discord.Member):
    await member.timeout(None)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}",
        title="Action: Untimeout"
    )

@bot.hybrid_command(name="purge", description="Deletes messages.")
@is_whitelisted()
@app_commands.describe(amount="Messages to delete (1-100)")
async def purge(ctx: commands.Context, amount: int):
    if amount < 1 or amount > 100:
        return await safe_respond(ctx, "**Limit Exceeded:** Value must be between 1 and 100.", ephemeral=True)
        
    if ctx.interaction:
        await ctx.interaction.response.defer(ephemeral=True)
        deleted = await ctx.channel.purge(limit=amount)
        await safe_respond(ctx, f"Purged `{len(deleted)}` messages.", ephemeral=True, title="Action: Purge")
    else:
        deleted = await ctx.channel.purge(limit=amount + 1)
        confirm = await ctx.send(embed=build_purple_embed(title="Action: Purge", description=f"Purged `{len(deleted) - 1}` messages."))
        await confirm.delete(delay=3)

@bot.hybrid_command(name="lock", description="Locks current channel.")
@is_whitelisted()
async def lock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = False
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await safe_respond(
        ctx, 
        f"**Channel:** {ctx.channel.mention}\n**Moderator:** {ctx.author.mention}",
        title="Action: Channel Locked"
    )

@bot.hybrid_command(name="unlock", description="Unlocks current channel.")
@is_whitelisted()
async def unlock(ctx: commands.Context):
    overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
    overwrite.send_messages = True
    await ctx.channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await safe_respond(
        ctx, 
        f"**Channel:** {ctx.channel.mention}\n**Moderator:** {ctx.author.mention}",
        title="Action: Channel Unlocked"
    )

@bot.hybrid_command(name="slowmode", description="Sets slowmode.")
@is_whitelisted()
@app_commands.describe(seconds="Delay in seconds")
async def slowmode(ctx: commands.Context, seconds: int):
    await ctx.channel.edit(slowmode_delay=seconds)
    await safe_respond(
        ctx, 
        f"**Channel:** {ctx.channel.mention}\n**Seconds:** `{seconds}s`\n**Moderator:** {ctx.author.mention}",
        title="Action: Slowmode"
    )

@bot.hybrid_command(name="warn", description="Issues a database-stored warning.")
@is_whitelisted()
@app_commands.describe(member="Target member", reason="Infraction reason")
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str):
    count = db_add_warning(ctx.guild.id, member.id, ctx.author.id, ctx.author.name, reason)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Total Infractions:** `{count}`\n**Moderator:** {ctx.author.mention}\n**Reason:** *{reason}*",
        title="Action: Warn"
    )

@bot.hybrid_command(name="warnings", description="Reads stored warnings.")
@is_whitelisted()
@app_commands.describe(member="Target member")
async def warnings(ctx: commands.Context, member: discord.Member):
    records = db_get_warnings(ctx.guild.id, member.id)
    if not records:
        return await safe_respond(ctx, f"**Clean Record:** {member.mention} has no stored infractions.")
        
    lines = [f"**Infractions for {member.name}** (`{member.id}`):"]
    for row in records:
        w_id, mod, reason, ts = row
        lines.append(f"`#{w_id}` — *{reason}* | Moderator: `{mod}` ({ts})")
    await safe_respond(ctx, "\n".join(lines), ephemeral=False, title="Infraction History")

@bot.hybrid_command(name="clearwarns", description="Clears stored warnings.")
@is_whitelisted()
@app_commands.describe(member="Target member")
async def clearwarns(ctx: commands.Context, member: discord.Member):
    deleted = db_clear_warnings(ctx.guild.id, member.id)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**Cleared:** `{deleted}` infractions\n**Moderator:** {ctx.author.mention}",
        title="Action: Clear Warnings"
    )

@bot.hybrid_command(name="nick", description="Changes member nickname.")
@is_whitelisted()
@app_commands.describe(member="Member", new_nick="New name (blank to clear)")
async def nick(ctx: commands.Context, member: discord.Member, *, new_nick: Optional[str] = None):
    allowed, msg = can_moderate(ctx, member)
    if not allowed:
        return await safe_respond(ctx, msg, ephemeral=True)
        
    await member.edit(nick=new_nick)
    await safe_respond(
        ctx, 
        f"**Target:** {member.mention} (`{member.id}`)\n**New Nick:** `{new_nick or 'Reset'}`\n**Moderator:** {ctx.author.mention}",
        title="Action: Nickname Update"
    )

@bot.hybrid_command(name="role", description="Assigns or strips role.")
@is_whitelisted()
@app_commands.describe(action="'add' or 'remove'", member="Target member", role="Target role")
async def role(ctx: commands.Context, action: str, member: discord.Member, role: discord.Role):
    if role >= ctx.guild.me.top_role:
        return await safe_respond(ctx, "**Hierarchy Error:** That role is equal to or higher than my top role.", ephemeral=True)
        
    action = action.lower().strip()
    if action == "add":
        await member.add_roles(role)
    elif action == "remove":
        await member.remove_roles(role)
    else:
        return await safe_respond(ctx, "**Syntax Error:** Specify `add` or `remove`.", ephemeral=True)
        
    await safe_respond(
        ctx, 
        f"**Action:** `{action.upper()}`\n**Role:** {role.name}\n**Target:** {member.mention} (`{member.id}`)\n**Moderator:** {ctx.author.mention}",
        title="Action: Role Modified"
    )

@bot.hybrid_command(name="nuke", description="Clones and deletes the channel to clear entire history.")
@is_whitelisted()
async def nuke(ctx: commands.Context):
    channel = ctx.channel
    pos = channel.position
    new_channel = await channel.clone(reason=f"Nuked by {ctx.author}")
    await channel.delete()
    await new_channel.edit(position=pos)
    embed = build_purple_embed(title="Channel Reset", description=f"Channel nuked by {ctx.author.mention}")
    f = get_footer_file()
    if f:
        await new_channel.send(embed=embed, file=f)
    else:
        await new_channel.send(embed=embed)

@bot.hybrid_command(name="lockdown", description="Locks down all public channels in the server.")
@is_whitelisted()
async def lockdown(ctx: commands.Context):
    locked = 0
    for ch in ctx.guild.text_channels:
        if ch.permissions_for(ctx.guild.default_role).send_messages:
            overwrite = ch.overwrites_for(ctx.guild.default_role)
            overwrite.send_messages = False
            await ch.set_permissions(ctx.guild.default_role, overwrite=overwrite)
            locked += 1
            
    await safe_respond(
        ctx, 
        f"**Channels Closed:** `{locked}`\n**Moderator:** {ctx.author.mention}",
        title="Alert: Server Lockdown"
    )

@bot.hybrid_command(name="unlockdown", description="Re-opens all channels after a lockdown.")
@is_whitelisted()
async def unlockdown(ctx: commands.Context):
    unlocked = 0
    for ch in ctx.guild.text_channels:
        overwrite = ch.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = True
        await ch.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        unlocked += 1
        
    await safe_respond(
        ctx, 
        f"**Channels Restored:** `{unlocked}`\n**Moderator:** {ctx.author.mention}",
        title="Alert: Server Unlockdown"
    )

@bot.hybrid_command(name="massban", description="Bans multiple user IDs separated by spaces or commas.")
@is_whitelisted()
@app_commands.describe(ids="List of user IDs")
async def massban(ctx: commands.Context, *, ids: str):
    parsed_ids = re.findall(r"\b\d{17,20}\b", ids)
    if not parsed_ids:
        return await safe_respond(ctx, "**Syntax Error:** No valid Discord snowflakes detected.", ephemeral=True)
    
    banned = []
    failed = []
    for user_id in parsed_ids:
        try:
            await ctx.guild.ban(discord.Object(id=int(user_id)), reason=f"Massban by {ctx.author}")
            banned.append(user_id)
        except Exception:
            failed.append(user_id)

    await safe_respond(
        ctx,
        f"**Success:** `{len(banned)}`\n**Failed:** `{len(failed)}`\n**Moderator:** {ctx.author.mention}",
        title="Action: Massban"
    )

@bot.hybrid_command(name="snipe", description="Recovers the most recently deleted message in this channel.")
@is_whitelisted()
async def snipe(ctx: commands.Context):
    data = snipe_cache.get(ctx.channel.id)
    if not data:
        return await safe_respond(ctx, "**Snipe:** No deleted messages recorded for this channel.", ephemeral=True)
    
    time_str = data["time"].strftime("%H:%M:%S UTC")
    msg = f"**Author:** `{data['author']}`\n**Time:** `{time_str}`\n**Message:** {data['content']}"
    await safe_respond(ctx, msg, ephemeral=False, title="Snipe Record")

@bot.hybrid_command(name="whois", description="Detailed profile inspection.")
@is_whitelisted()
@app_commands.describe(member="Member to inspect")
async def whois(ctx: commands.Context, member: discord.Member):
    created = int(member.created_at.timestamp())
    joined = int(member.joined_at.timestamp()) if member.joined_at else 0
    roles = [r.mention for r in member.roles if r != ctx.guild.default_role]
    roles_str = ", ".join(roles) if roles else "None"

    response = (
        f"**User:** {member.mention} (`{member.id}`)\n"
        f"**Registered:** <t:{created}:F> (<t:{created}:R>)\n"
        f"**Joined Server:** <t:{joined}:F> (<t:{joined}:R>)\n"
        f"**Highest Role:** {member.top_role.mention}\n"
        f"**Roles:** {roles_str}"
    )
    await safe_respond(ctx, response, ephemeral=False, title="User Inspection")

@bot.hybrid_command(name="avatar", description="Fetches member avatar.")
@is_whitelisted()
@app_commands.describe(member="Target member")
async def avatar(ctx: commands.Context, member: discord.Member):
    embed = build_purple_embed(title=f"Avatar: {member.name}")
    embed.set_image(url=member.display_avatar.url)
    if ctx.interaction:
        await ctx.interaction.response.send_message(embed=embed)
    else:
        await ctx.send(embed=embed)

@bot.hybrid_command(name="modhelp", description="Lists commands.")
async def modhelp(ctx: commands.Context):
    help_text = """
**Enforcement:**
`kick <user> [reason]` — Kick member
`ban <user> [days] [reason]` — Ban member
`unban <id> [reason]` — Revoke ban via ID
`softban <user> [reason]` — Ban & unban to wipe 7d chat
`timeout <user> <min> [reason]` — Mute member
`untimeout <user>` — Remove timeout
`purge <amount>` — Bulk clear messages
`warn <user> <reason>` — File stored infraction
`warnings <user>` — View infraction history
`clearwarns <user>` — Wipe user infractions

**Channel & Raid Defense:**
`say <text> [channel]` — Broadcast message
`lock` / `unlock` — Toggle public messaging
`slowmode <sec>` — Change rate limit
`nuke` — Recreate channel to wipe chat instantly
`lockdown` / `unlockdown` — Global server lock toggle
`massban <ids...>` — Bulk bans a list of IDs

**Investigation & Utility:**
`whois <user>` — Account details & timestamps
`snipe` — Retrieve last deleted message
`avatar <user>` — View profile picture
`nick <user> [name]` — Change nickname
`role <add/remove> <user> <role>` — Modify roles
"""
    await safe_respond(ctx, help_text, ephemeral=False, title="Moderation & Utilities")

if not TOKEN:
    print("[CRITICAL] TOKEN not found in .env.", flush=True)
else:
    bot.run(TOKEN)