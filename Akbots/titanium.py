# Akbots - Don't Remove Credit - @AkBots_Official
#
# Titanium Clone Mode — ported from fwdbot's plugins/titanium.py, trimmed
# to fit Akbots. Two things from the original were intentionally NOT
# ported:
#   - Plan-based slot limits (Config.TITANIUM_PLAN_LIMITS) — Akbots has no
#     subscription-tier system, so this uses one flat MAX_TITANIUM_BOTS cap
#     instead. If Akbots ever gets tiers, gate this the same way.
#   - Bot API 9.6 "Managed Bots" auto-create (the deep-link "tap Create,
#     zero BotFather" flow) — that needs can_manage_bots enabled on the
#     main bot plus a manager-bot poller (fwdbot's utils/managed_bots.py),
#     neither of which exist here. Connecting is via a normal @BotFather
#     token instead — this was fwdbot's own fallback path when Bot
#     Management Mode isn't set up, so it's a well-tested route either way.
#
# What it does, in two parts:
#   1. Flood-pool sharing (original behaviour) — get_job_client() picks a
#      connected clone over the main bot for a job when it can reach the
#      same chats, spreading rate limits across bots.
#   2. Personal forward-bot (new) — once connected, a clone also answers
#      a hand-picked set of forward-tool commands (/fwd, /setsource,
#      /settarget, /channels, etc. — see PERSONAL_BOT_COMMANDS below)
#      DIRECTLY, so the owner can talk to their own bot instead of the
#      shared Akbots one. It reads/writes the exact same per-user data
#      (fwd_source, fwd_routes, ...) as the main bot, so it's really the
#      same account, just reachable through a bot token only its owner
#      controls.
#
# CRITICAL SAFETY NOTE: clone Clients below are still started WITHOUT
# plugins=dict(root="Akbots") — that auto-loads and attaches EVERY
# handler in this package to whatever Client instantiates it, with none
# of them owner-scoped (a lot of Akbots' ~50 other plugins only check
# "does this Telegram user have an account", not "is this user the
# person who owns this specific bot token"). Doing that here would turn
# every connected clone into its own fully public, unrestricted copy of
# the ENTIRE bot for anyone who finds its username — not just its owner.
#
# Instead, _attach_personal_bot() below manually re-registers a small,
# reviewed set of already-existing forward.py/channels.py command
# functions onto the clone, each wrapped in an explicit
# filters.user(owner_id) check. Nothing outside that hand-picked list
# runs on a clone. Extending PERSONAL_BOT_COMMANDS to cover more of
# Akbots later is possible, but each addition should get the same
# "is this actually safe to expose owner-scoped, unaudited" look this
# list got — don't just point it at plugins=dict(root="Akbots").
#
# The clone bot still has to be manually added (as member/admin) to
# whatever chats a job touches, exactly like the main bot — connecting it
# here doesn't grant it access to anything by itself.

import time
from pyrogram import Client, filters, enums
from pyrogram.errors import RPCError
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.types import Message
from config import API_ID, API_HASH
from database.db import db
from Akbots.direct_utils import E_CHECK, E_CROSS, E_INFO
from logger import LOGGER

logger = LOGGER(__name__)

MAX_TITANIUM_BOTS = 5  # flat cap — see module docstring re: no plan system here

_CLONE_CACHE = {}       # token -> connected Client, reused across jobs/messages
_PERSONAL_ATTACHED = set()  # tokens that already have personal-bot handlers wired (avoid double-attach)


async def _get_clone_client(token: str) -> Client:
    cached = _CLONE_CACHE.get(token)
    if cached is not None and cached.is_connected:
        return cached
    client = Client(
        f"titanium_{token[:10]}",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=token,
        in_memory=True,
        max_concurrent_transmissions=10,
        # no plugins= here on purpose — see module docstring
    )
    await client.start()
    _CLONE_CACHE[token] = client
    return client


def _owner_wrap(fn, owner_id: int):
    """Wraps an already-existing handler function so it only runs for
    messages/callbacks from owner_id. The wrapped function is the exact
    same code path the main bot uses for that command — nothing is
    reimplemented here, just gated."""
    async def _wrapped(client, update):
        sender = getattr(update, "from_user", None)
        if sender is None or sender.id != owner_id:
            return
        await fn(client, update)
    return _wrapped


async def _attach_personal_bot(clone: Client, owner_id: int, token: str):
    """Registers the hand-picked PERSONAL_BOT_COMMANDS set of forward.py /
    channels.py handlers on `clone`, each gated to owner_id. Safe to call
    more than once for the same token — a no-op after the first time
    (tracked via _PERSONAL_ATTACHED) so reconnecting on boot or from
    get_job_client doesn't stack duplicate handlers and double-reply."""
    if token in _PERSONAL_ATTACHED:
        return

    # Imported here, not at module top, to avoid a circular import —
    # forward.py imports `from Akbots import titanium` for get_job_client.
    from Akbots import forward as _fwd
    from Akbots import channels as _chan

    command_handlers = [
        ("start", _personal_start_cmd),
        ("setsource", _fwd.setsource_cmd),
        ("settarget", _fwd.settarget_cmd),
        ("fwd", _fwd.fwd_cmd),
        ("fwdresume", _fwd.fwdresume_cmd),
        ("fwdstatus", _fwd.fwdstatus_cmd),
        ("fwdcancel", _fwd.fwdcancel_cmd),
        ("fwd_settings", _fwd.fwd_settings_cmd),
        ("fwd_caption", _fwd.fwd_caption_cmd),
        ("fwd_button", _fwd.fwd_button_cmd),
        ("fwd_filter", _fwd.fwd_filter_cmd),
        ("channels", _chan.channels_cmd),
        ("addroute", _chan.addroute_cmd),
        ("delroute", _chan.delroute_cmd),
    ]
    for cmd_name, fn in command_handlers:
        clone.add_handler(MessageHandler(
            _owner_wrap(fn, owner_id),
            filters.private & filters.command(cmd_name)
        ))

    callback_handlers = [
        (r"^fwds:(source|target|caption|button|filters|resetprogress|back|close)$", _fwd.fwd_settings_callbacks),
        (r"^route_go#", _chan.route_go_callback),
        (r"^route_del#", _chan.route_del_callback),
    ]
    for pattern, fn in callback_handlers:
        clone.add_handler(CallbackQueryHandler(
            _owner_wrap(fn, owner_id),
            filters.regex(pattern)
        ))

    _PERSONAL_ATTACHED.add(token)
    logger.info(f"Personal-bot handlers attached for owner {owner_id} on clone token ...{token[-6:]}")


async def _personal_start_cmd(client: Client, message: Message):
    me = await client.get_me()
    await message.reply_text(
        f"<b>{E_CHECK} @{me.username} is your personal Akbots forward-bot.</b>\n\n"
        f"It shares your saved settings with the main Akbots bot — /fwd, /setsource, "
        f"/settarget, /channels, /fwd_settings all work here the same way, just on your "
        f"own bot token.\n\n"
        f"<i>Manage this connection (or add more) from the main Akbots bot with /titanium.</i>",
        parse_mode=enums.ParseMode.HTML
    )


async def boot_personal_bots():
    """Reconnects every user's saved Titanium bots and re-wires their
    personal-bot handlers. Call once from bot.py's startup — without
    this, a connected clone stops answering commands after every
    process restart until its owner happens to trigger get_job_client
    (e.g. by running a forward job), which is the only other place a
    clone gets reconnected.
    """
    connected = 0
    try:
        users = await db.get_all_users()
        async for user in users:
            bots = user.get("titanium_bots", [])
            if not bots:
                continue
            owner_id = user["id"]
            for b in bots:
                try:
                    clone = await _get_clone_client(b["token"])
                    await _attach_personal_bot(clone, owner_id, b["token"])
                    connected += 1
                except Exception as e:
                    logger.warning(f"Titanium boot: couldn't reconnect @{b.get('username', '?')} for {owner_id}: {e}")
    except Exception as e:
        logger.error(f"Titanium boot_personal_bots failed: {e}")
    if connected:
        logger.info(f"Titanium: {connected} personal clone bot(s) reconnected on boot.")
    return connected


@Client.on_message(filters.private & filters.command("titanium"))
async def titanium_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    bots = await db.get_titanium_bots(user_id)
    lines = [
        "<b>⚡ Titanium Clone Mode</b>",
        "",
        "Connect your own @BotFather bot(s) so your jobs run on a separate "
        "flood-limit pool instead of sharing the main bot's with everyone else. "
        "Each connected bot also becomes your own personal forward-bot — talk to "
        "it directly with /fwd, /setsource, /settarget, /channels, /fwd_settings.",
        "",
        f"<b>Connected:</b> {len(bots)}/{MAX_TITANIUM_BOTS}",
    ]
    for b in bots:
        lines.append(f"  • @{b['username']}")
    lines += [
        "",
        "<code>/addbot &lt;token&gt;</code> — connect a bot (get one from @BotFather → /newbot)",
        "<code>/delbot &lt;username&gt;</code> — disconnect one",
        "",
        "<i>Add each clone as admin to whatever chats you use it for — connecting "
        "it here doesn't give it access to anything on its own.</i>",
    ]
    await message.reply_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command("addbot"))
async def addbot_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/addbot 123456:ABC-your-bot-token</code>\n"
            f"<i>Create one with @BotFather (/newbot) first, then paste the token here.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    token = message.command[1].strip()
    bots = await db.get_titanium_bots(user_id)
    if len(bots) >= MAX_TITANIUM_BOTS:
        return await message.reply_text(
            f"<b>{E_CROSS} Limit reached</b> ({MAX_TITANIUM_BOTS} bots). Disconnect one with /delbot first.",
            parse_mode=enums.ParseMode.HTML
        )
    if any(b["token"] == token for b in bots):
        return await message.reply_text(f"<b>{E_INFO} That bot is already connected.</b>", parse_mode=enums.ParseMode.HTML)

    status = await message.reply_text(f"<b>{E_INFO} Verifying token...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        test_client = Client(
            f"titanium_verify_{user_id}_{int(time.time())}",
            api_id=API_ID, api_hash=API_HASH, bot_token=token, in_memory=True
        )
        await test_client.start()
        me = await test_client.get_me()
        await test_client.stop()
    except Exception as e:
        return await status.edit_text(f"<b>{E_CROSS} Invalid token:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)

    if any(b["username"] == me.username for b in bots):
        return await status.edit_text(f"<b>{E_INFO} @{me.username} is already connected.</b>", parse_mode=enums.ParseMode.HTML)

    await db.add_titanium_bot(user_id, token, me.username)
    try:
        clone = await _get_clone_client(token)
        await _attach_personal_bot(clone, user_id, token)
        personal_note = f"\n<i>@{me.username} is now also your personal forward-bot — try /start on it.</i>"
    except Exception as e:
        logger.warning(f"addbot: personal-bot attach failed for @{me.username}: {e}")
        personal_note = ""
    await status.edit_text(
        f"<b>{E_CHECK} Connected @{me.username}.</b>\n"
        f"<i>Add it as admin to your chats — it'll be picked up automatically for jobs that can use it.</i>"
        f"{personal_note}",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("delbot"))
async def delbot_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if len(message.command) < 2:
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/delbot username</code>", parse_mode=enums.ParseMode.HTML)
    username = message.command[1].strip().lstrip("@")

    bots = await db.get_titanium_bots(user_id)
    match = next((b for b in bots if b["username"] == username), None)

    removed = await db.remove_titanium_bot(user_id, username)
    if not removed:
        return await message.reply_text(f"<b>{E_INFO} No connected bot found with that username.</b>", parse_mode=enums.ParseMode.HTML)

    # Stop it from listening entirely — otherwise it keeps answering
    # /fwd etc. (still correctly, since the DB record is gone this just
    # means "as if just reconnected" — but simplest and safest is to
    # shut it down until re-added).
    if match:
        token = match["token"]
        _PERSONAL_ATTACHED.discard(token)
        cached = _CLONE_CACHE.pop(token, None)
        if cached is not None:
            try:
                await cached.stop()
            except Exception as e:
                logger.debug(f"delbot: stop() on evicted clone failed (likely already stopped): {e}")

    await message.reply_text(f"<b>{E_CHECK} Disconnected @{username}.</b>", parse_mode=enums.ParseMode.HTML)


async def get_job_client(user_id: int, fallback_client: Client, *chats_to_check):
    """Picks the least-recently-used client — main bot or a connected
    Titanium clone — that can access every chat in chats_to_check. Falls
    back to fallback_client if the person has no clones connected, or if
    none of them (nor the main bot) can reach every chat listed.

    Returns (client, is_clone: bool, username: str|None).

    This is the integration point other plugins call into — currently
    wired into Akbots/forward.py's job launch. Other long-running plugins
    (ytdl.py, terabox.py, etc.) can call this the same way to get the same
    flood-pool benefit; that wasn't done for all of them in this pass to
    keep the change reviewable.
    """
    bots = await db.get_titanium_bots(user_id)
    if not bots:
        return fallback_client, False, None

    candidates = [("__main__", fallback_client, None)]
    for b in sorted(bots, key=lambda x: x.get("last_used", 0)):
        try:
            clone = await _get_clone_client(b["token"])
            await _attach_personal_bot(clone, user_id, b["token"])
            candidates.append((b["token"], clone, b["username"]))
        except Exception:
            continue

    for token, cand_client, username in candidates:
        try:
            for chat in chats_to_check:
                await cand_client.get_chat(chat)
        except RPCError:
            continue
        if token != "__main__":
            await db.touch_titanium_bot(user_id, token)
        return cand_client, token != "__main__", username

    return fallback_client, False, None
