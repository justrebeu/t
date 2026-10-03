import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

PREFIX = "+"
CONFIG_FILE = "config.json"

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.moderation = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# --------------------------- Configuration ---------------------------

DEFAULTS = {
    "installed": False,
    "log_channel_id": None,
    "max_deletes": 3,          # suppressions max (salons / rôles)...
    "max_creates": 3,          # créations max (salons / rôles)...
    "window": 10,              # ... sur cette fenêtre (secondes)
    "min_account_age_days": 7,  # comptes plus récents = suspects
    "spam_messages": 5,        # messages max...
    "spam_window": 5,          # ... sur cette fenêtre (secondes)
    "timeout_seconds": 60,     # sanction anti-spam : 1 minute
    "whitelist": [],           # IDs de confiance
}
DEFAULT_STATS = {"blocked_accounts": 0, "spam_sanctions": 0, "raid_sanctions": 0}


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


config = load_config()


def save_config() -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_conf(guild_id: int) -> dict:
    return {**DEFAULTS, **config.get(str(guild_id), {})}


def set_conf(guild_id: int, **values) -> None:
    entry = config.setdefault(str(guild_id), {})
    entry.update(values)
    save_config()


def get_stats(guild_id: int) -> dict:
    entry = config.setdefault(str(guild_id), {})
    return {**DEFAULT_STATS, **entry.get("stats", {})}


def bump(guild_id: int, key: str) -> None:
    entry = config.setdefault(str(guild_id), {})
    stats = {**DEFAULT_STATS, **entry.get("stats", {})}
    stats[key] += 1
    entry["stats"] = stats
    save_config()


def get_infractions(guild_id: int, user_id: int) -> int:
    return config.get(str(guild_id), {}).get("infractions", {}).get(str(user_id), 0)


def add_infraction(guild_id: int, user_id: int) -> None:
    entry = config.setdefault(str(guild_id), {})
    infractions = entry.setdefault("infractions", {})
    infractions[str(user_id)] = infractions.get(str(user_id), 0) + 1
    save_config()


# ----------------------------- Utilitaires -----------------------------

counters: dict = defaultdict(list)


def hit(key, window: int) -> int:
    now = time.time()
    counters[key] = [t for t in counters[key] if now - t < window]
    counters[key].append(now)
    return len(counters[key])


async def log(guild: discord.Guild, title: str, description: str, color=0xE67E22):
    conf = get_conf(guild.id)
    if not conf["log_channel_id"]:
        return
    channel = guild.get_channel(conf["log_channel_id"])
    if channel is None:
        return
    embed = discord.Embed(title=title, description=description, color=color)
    embed.timestamp = datetime.now(timezone.utc)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


def is_trusted(guild: discord.Guild, user_id: int) -> bool:
    conf = get_conf(guild.id)
    return (
        user_id == guild.owner_id
        or user_id == bot.user.id
        or user_id in conf["whitelist"]
    )


async def get_executor(guild: discord.Guild, action: discord.AuditLogAction, target_id: int):
    """Retrouve l'auteur d'une action grâce au journal d'audit."""
    try:
        async for entry in guild.audit_logs(limit=5, action=action):
            age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
            if entry.target is not None and entry.target.id == target_id and age < 5:
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None


async def punish(guild: discord.Guild, user_id: int, reason: str):
    bump(guild.id, "raid_sanctions")
    add_infraction(guild.id, user_id)
    try:
        await guild.ban(discord.Object(id=user_id), reason=f"[Protect] {reason}")
        await log(guild, "🔨 Utilisateur banni", f"<@{user_id}>\n**Raison :** {reason}", 0xE74C3C)
    except discord.HTTPException:
        member = guild.get_member(user_id)
        if member:
            try:
                await member.edit(roles=[], reason=f"[Protect] {reason}")
                await log(guild, "⚠️ Rôles retirés", f"<@{user_id}>\n**Raison :** {reason}", 0xE74C3C)
            except discord.HTTPException:
                pass


async def guard_action(guild, action, target_id, target_name, kind, label, on_exceed):
    conf = get_conf(guild.id)
    if not conf["installed"]:
        return

    executor = await get_executor(guild, action, target_id)
    await log(
        guild,
        f"📝 {label}",
        f"**Cible :** {target_name}\n**Auteur :** {executor.mention if executor else 'inconnu'}",
    )

    if executor is None or is_trusted(guild, executor.id):
        return

    limit = conf["max_deletes"] if kind == "delete" else conf["max_creates"]
    count = hit((guild.id, executor.id, kind), conf["window"])
    if count > limit:
        await on_exceed(executor)


# ------------------ Anti suppression / création massive ------------------

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild

    async def exceed(executor):
        await punish(guild, executor.id, "Suppression massive de salons")
        try:
            await channel.clone(reason="[Protect] Restauration")
        except discord.HTTPException:
            pass

    await guard_action(guild, discord.AuditLogAction.channel_delete, channel.id,
                       channel.name, "delete", "Salon supprimé", exceed)


@bot.event
async def on_guild_role_delete(role):
    guild = role.guild

    async def exceed(executor):
        await punish(guild, executor.id, "Suppression massive de rôles")
        try:
            await guild.create_role(
                name=role.name,
                colour=role.colour,
                hoist=role.hoist,
                mentionable=role.mentionable,
                permissions=role.permissions,
                reason="[Protect] Restauration",
            )
        except discord.HTTPException:
            pass

    await guard_action(guild, discord.AuditLogAction.role_delete, role.id,
                       role.name, "delete", "Rôle supprimé", exceed)


@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild

    async def exceed(executor):
        await punish(guild, executor.id, "Création rapide de salons")
        try:
            await channel.delete(reason="[Protect] Création rapide")
        except discord.HTTPException:
            pass

    await guard_action(guild, discord.AuditLogAction.channel_create, channel.id,
                       channel.name, "create", "Salon créé", exceed)


@bot.event
async def on_guild_role_create(role):
    guild = role.guild

    async def exceed(executor):
        await punish(guild, executor.id, "Création rapide de rôles")
        try:
            await role.delete(reason="[Protect] Création rapide")
        except discord.HTTPException:
            pass

    await guard_action(guild, discord.AuditLogAction.role_create, role.id,
                       role.name, "create", "Rôle créé", exceed)


# ------------------------- Comptes suspects -------------------------

@bot.event
async def on_member_join(member: discord.Member):
    conf = get_conf(member.guild.id)
    if not conf["installed"] or member.bot or is_trusted(member.guild, member.id):
        return

    age_days = (datetime.now(timezone.utc) - member.created_at).total_seconds() / 86400
    if age_days < conf["min_account_age_days"]:
        try:
            await member.send(
                f"Ton compte est trop récent pour rejoindre **{member.guild.name}** "
                f"(minimum {conf['min_account_age_days']} jours)."
            )
        except discord.HTTPException:
            pass
        try:
            await member.kick(reason="[Protect] Compte suspect (trop récent)")
        except discord.HTTPException:
            return
        bump(member.guild.id, "blocked_accounts")
        add_infraction(member.guild.id, member.id)
        await log(
            member.guild,
            "🚫 Compte suspect bloqué",
            f"{member} ({member.id})\nÂge du compte : {age_days:.1f} jour(s)",
            0xE74C3C,
        )


# ------------------------------ Snipe ------------------------------

snipes: dict = {}


@bot.event
async def on_message_delete(message: discord.Message):
    if message.guild is None or message.author.bot:
        return
    snipes[message.channel.id] = {
        "content": message.content or "*(aucun texte)*",
        "author": message.author,
        "image": message.attachments[0].url if message.attachments else None,
        "at": datetime.now(timezone.utc),
    }
    await log(
        message.guild,
        "🗑️ Message supprimé",
        f"**Auteur :** {message.author.mention}\n**Salon :** {message.channel.mention}\n"
        f"**Contenu :** {(message.content or '*(aucun texte)*')[:1000]}",
        0x95A5A6,
    )


@bot.command(name="snipe")
@commands.guild_only()
async def snipe(ctx: commands.Context):
    if not ctx.author.guild_permissions.manage_messages:
        return await ctx.reply("❌ Tu as besoin de la permission **Gérer les messages**.")
    data = snipes.get(ctx.channel.id)
    if not data:
        return await ctx.reply("Aucun message supprimé récemment dans ce salon.")
    embed = discord.Embed(description=data["content"], color=0x3498DB)
    embed.set_author(name=str(data["author"]), icon_url=data["author"].display_avatar.url)
    embed.timestamp = data["at"]
    if data["image"]:
        embed.set_image(url=data["image"])
    await ctx.reply(embed=embed)


# ----------------------------- Anti-spam -----------------------------

spam_tracker: dict = defaultdict(list)


async def check_spam(message: discord.Message):
    guild = message.guild
    conf = get_conf(guild.id)
    if not conf["installed"] or is_trusted(guild, message.author.id):
        return

    key = (guild.id, message.author.id)
    now = time.time()
    entries = [(t, m) for t, m in spam_tracker[key] if now - t < conf["spam_window"]]
    entries.append((now, message))
    spam_tracker[key] = entries

    if len(entries) >= conf["spam_messages"]:
        spam_tracker.pop(key, None)
        await asyncio.gather(*(m.delete() for _, m in entries), return_exceptions=True)
        try:
            await message.author.timeout(
                timedelta(seconds=conf["timeout_seconds"]), reason="[Protect] Spam"
            )
        except discord.HTTPException:
            return
        bump(guild.id, "spam_sanctions")
        add_infraction(guild.id, message.author.id)
        try:
            await message.channel.send(
                f"🔇 {message.author.mention} a été mute {conf['timeout_seconds']}s pour spam."
            )
        except discord.HTTPException:
            pass
        await log(
            guild,
            "🔇 Sanction anti-spam",
            f"{message.author.mention} ({message.author.id}) mute "
            f"{conf['timeout_seconds']}s dans {message.channel.mention}",
        )


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    await check_spam(message)
    await bot.process_commands(message)


# ------------------------------ Commandes ------------------------------

REQUIRED_PERMS = {
    "view_audit_log": "Voir les logs d'audit",
    "manage_channels": "Gérer les salons",
    "manage_roles": "Gérer les rôles",
    "manage_messages": "Gérer les messages",
    "ban_members": "Bannir des membres",
    "kick_members": "Expulser des membres",
    "moderate_members": "Mettre en sourdine",
}


@bot.group(name="protect", invoke_without_command=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def protect(ctx: commands.Context):
    await ctx.reply("Usage : `+protect install` | `+protect whitelist <user>` | `+security status`")


@protect.command(name="install")
async def protect_install(ctx: commands.Context):
    guild = ctx.guild
    perms = guild.me.guild_permissions
    missing = [label for attr, label in REQUIRED_PERMS.items() if not getattr(perms, attr)]
    if missing:
        return await ctx.reply("❌ Il me manque des permissions : " + ", ".join(f"`{m}`" for m in missing))

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True),
    }
    log_channel = await guild.create_text_channel("protect-logs", overwrites=overwrites)
    set_conf(guild.id, installed=True, log_channel_id=log_channel.id)

    conf = get_conf(guild.id)
    embed = discord.Embed(title="🛡️ Protection installée", color=0x2ECC71)
    embed.description = "\n".join([
        f"• Logs : {log_channel.mention}",
        f"• Anti-suppression : max **{conf['max_deletes']}** salons/rôles en {conf['window']}s",
        f"• Anti-création : max **{conf['max_creates']}** salons/rôles en {conf['window']}s",
        f"• Comptes de moins de **{conf['min_account_age_days']} jours** bloqués",
        f"• Anti-spam : **{conf['spam_messages']}** messages en {conf['spam_window']}s "
        f"→ mute {conf['timeout_seconds']}s",
        "• Snipe : `+snipe`",
        "• Statut : `+security status`",
    ])
    await ctx.reply(embed=embed)


@protect.command(name="whitelist")
async def protect_whitelist(ctx: commands.Context, user: discord.User):
    conf = get_conf(ctx.guild.id)
    wl = list(conf["whitelist"])
    if user.id not in wl:
        wl.append(user.id)
    set_conf(ctx.guild.id, whitelist=wl)
    await ctx.reply(f"✅ {user.mention} est maintenant de confiance.")


@bot.group(name="security", invoke_without_command=True)
@commands.guild_only()
@commands.has_permissions(administrator=True)
async def security(ctx: commands.Context):
    await ctx.reply("Usage : `+security status`")


@security.command(name="status")
async def security_status(ctx: commands.Context):
    guild = ctx.guild
    conf = get_conf(guild.id)
    stats = get_stats(guild.id)
    perms = guild.me.guild_permissions

    on = "✅ Actif" if conf["installed"] else "❌ Non installé (`+protect install`)"
    log_ch = f"<#{conf['log_channel_id']}>" if conf["log_channel_id"] else "—"
    top_ok = guild.me.top_role == guild.roles[-1]

    embed = discord.Embed(
        title="🛡️ Statut de la sécurité",
        color=0x2ECC71 if conf["installed"] else 0xE74C3C,
    )
    embed.add_field(name="Protection", value=on, inline=True)
    embed.add_field(name="Salon de logs", value=log_ch, inline=True)
    embed.add_field(name="Latence", value=f"{round(bot.latency * 1000)} ms", inline=True)

    state = "✅" if conf["installed"] else "⏸️"
    embed.add_field(
        name="Modules",
        value="\n".join([
            f"{state} Logs & snipe",
            f"{state} Anti-suppression massive (>{conf['max_deletes']} / {conf['window']}s)",
            f"{state} Anti-création rapide (>{conf['max_creates']} / {conf['window']}s)",
            f"{state} Comptes suspects (<{conf['min_account_age_days']} jours)",
            f"{state} Anti-spam ({conf['spam_messages']} msg / {conf['spam_window']}s "
            f"→ mute {conf['timeout_seconds']}s)",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Permissions du bot",
        value="\n".join(
            f"{'✅' if getattr(perms, attr) else '❌'} {label}"
            for attr, label in REQUIRED_PERMS.items()
        ),
        inline=True,
    )
    embed.add_field(
        name="Statistiques",
        value="\n".join([
            f"Comptes bloqués : **{stats['blocked_accounts']}**",
            f"Sanctions spam : **{stats['spam_sanctions']}**",
            f"Raids sanctionnés : **{stats['raid_sanctions']}**",
            f"Utilisateurs de confiance : **{len(conf['whitelist'])}**",
        ]),
        inline=True,
    )
    if not top_ok:
        embed.add_field(
            name="⚠️ Hiérarchie",
            value="Mon rôle n'est pas tout en haut : je ne pourrai pas sanctionner certains membres.",
            inline=False,
        )
    await ctx.reply(embed=embed)


# --------------------- Système de confiance (+co) ---------------------

def trust_report(member: discord.Member) -> dict:
    """Calcule un score de confiance (0-100) à partir de signaux simples."""
    guild = member.guild
    conf = get_conf(guild.id)
    now = datetime.now(timezone.utc)

    if member.id == guild.owner_id or member.id in conf["whitelist"]:
        return {"special": True, "score": 100, "details": ["Propriétaire ou utilisateur de confiance"]}

    risk = 0
    details = []

    age_days = (now - member.created_at).total_seconds() / 86400
    if age_days < 1:
        risk += 40
        details.append(f"🔴 Compte créé il y a moins d'un jour (+40)")
    elif age_days < conf["min_account_age_days"]:
        risk += 30
        details.append(f"🟠 Compte très récent : {age_days:.0f} jour(s) (+30)")
    elif age_days < 30:
        risk += 15
        details.append(f"🟡 Compte récent : {age_days:.0f} jours (+15)")
    elif age_days < 90:
        risk += 5
        details.append(f"🟡 Compte de {age_days:.0f} jours (+5)")
    else:
        details.append(f"🟢 Compte ancien : {age_days:.0f} jours (+0)")

    if member.avatar is None:
        risk += 10
        details.append("🟡 Pas de photo de profil (+10)")

    if member.joined_at:
        joined_days = (now - member.joined_at).total_seconds() / 86400
        if joined_days < 1:
            risk += 15
            details.append("🟠 Arrivé sur le serveur il y a moins d'un jour (+15)")
        elif joined_days < 7:
            risk += 8
            details.append(f"🟡 Arrivé il y a {joined_days:.0f} jour(s) (+8)")
        else:
            details.append(f"🟢 Membre depuis {joined_days:.0f} jours (+0)")

    infractions = get_infractions(guild.id, member.id)
    if infractions:
        pts = min(infractions * 15, 45)
        risk += pts
        details.append(f"🔴 {infractions} sanction(s) du bot (+{pts})")

    if member.is_timed_out():
        risk += 10
        details.append("🟠 Actuellement en sourdine (+10)")

    score = max(0, 100 - risk)
    return {"special": False, "score": score, "details": details}


def trust_level(score: int):
    if score >= 80:
        return "🟢 Normal", 0x2ECC71
    if score >= 60:
        return "🟡 À surveiller", 0xF1C40F
    if score >= 40:
        return "🟠 Suspect", 0xE67E22
    return "🔴 Critique", 0xE74C3C


@bot.command(name="co")
@commands.guild_only()
@commands.has_permissions(manage_messages=True)
async def co(ctx: commands.Context, member: discord.Member = None):
    member = member or ctx.author
    report = trust_report(member)
    score = report["score"]

    if report["special"]:
        level, color = "💎 De confiance", 0x9B59B6
    else:
        level, color = trust_level(score)

    filled = score // 10
    bar = "█" * filled + "░" * (10 - filled)

    embed = discord.Embed(title=f"🔎 Confiance : {member}", color=color)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Niveau", value=level, inline=True)
    embed.add_field(name="Score", value=f"`{bar}` **{score}/100**", inline=True)
    embed.add_field(name="Détails", value="\n".join(report["details"]), inline=False)
    if member.bot:
        embed.set_footer(text="Ce compte est un bot.")
    await ctx.reply(embed=embed)


# ----------------------------- +slowmode -----------------------------

def parse_duration(text: str) -> int:
    text = text.strip().lower()
    if text in ("off", "0", "none", "non", "stop"):
        return 0
    units = {"s": 1, "m": 60, "h": 3600}
    if text[-1] in units:
        return int(text[:-1]) * units[text[-1]]
    return int(text)  # sans unité = secondes


def format_duration(seconds: int) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}min"
    return f"{seconds}s"


@bot.command(name="slowmode")
@commands.guild_only()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, duration: str = None):
    channel = ctx.channel
    if duration is None:
        current = channel.slowmode_delay
        text = format_duration(current) if current else "désactivé"
        return await ctx.reply(
            f"⏱️ Slowmode actuel : **{text}**\nUsage : `+slowmode 5s`, `+slowmode 10m`, `+slowmode 1h`, `+slowmode off`"
        )
    try:
        seconds = parse_duration(duration)
    except (ValueError, IndexError):
        return await ctx.reply("❌ Durée invalide. Exemples : `5s`, `10m`, `1h`, `off`.")
    if seconds < 0 or seconds > 21600:
        return await ctx.reply("❌ La durée doit être comprise entre 0 et 6h.")
    try:
        await channel.edit(slowmode_delay=seconds, reason=f"[Protect] Slowmode par {ctx.author}")
    except discord.HTTPException:
        return await ctx.reply("❌ Je n'ai pas la permission de modifier ce salon.")
    if seconds == 0:
        await ctx.reply("✅ Slowmode désactivé.")
    else:
        await ctx.reply(f"✅ Slowmode réglé sur **{format_duration(seconds)}**.")
    await log(
        ctx.guild,
        "⏱️ Slowmode modifié",
        f"{ctx.channel.mention} → {format_duration(seconds) if seconds else 'désactivé'} par {ctx.author.mention}",
    )


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingPermissions):
        perms = ", ".join(error.missing_permissions)
        await ctx.reply(f"❌ Permission manquante : `{perms}`.")
    elif isinstance(error, (commands.CommandNotFound, commands.NoPrivateMessage)):
        return
    elif isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
        await ctx.reply("❌ Argument invalide ou manquant.")
    else:
        raise error


@bot.event
async def on_ready():
    print(f"Connecté en tant que {bot.user} ({bot.user.id})")


bot.run(os.getenv("TOKEN"))
