# Akbots - Don't Remove Credit - @AkBots_Official
#
# Forward tool — ported/simplified from the fwdbot project's core
# source->target forwarding engine (fwdbot's ak_manager.py / regix.py),
# rebuilt to fit Akbots as a single bot-token client (fwdbot's clone-bot /
# multi-tenant / subscription-tier machinery is intentionally NOT ported —
# it doesn't apply here).
#
# Uses copy_message (not forward_messages) for every message, same choice
# fwdbot makes, because copy_message re-sends the content instead of
# relaying it, so it also works on chats with forwarding restricted.
#
# USERBOT FALLBACK: the bot token alone can only touch chats it has been
# added to. For a private chat the bot isn't (and can't be) added to,
# /setsource and /settarget fall back to the user's own account — reusing
# the exact session string already stored by Akbots/session.py's /login
# flow (db.get_session), same pattern start.py already uses for restricted
# saves (Client(session_string=..., in_memory=True)). No new login system
# was built here; if the person hasn't run /login yet, they're told to.
#
# A single forward job needs ONE client that can read the source AND write
# the target, so at launch time we work out whether the bot alone can do
# both, or whether the personal account (userbot) has to do both — mixing
# (bot for one side, userbot for the other) isn't possible for copy_message
# and is reported back to the user as a clear limitation rather than
# silently failing.
#
# Commands:
#   /setsource <chat_id or @username>
#   /settarget <chat_id or @username>
#   /fwd <start_msg_id> <end_msg_id>   — forwards that id range, source->target
#   /fwdresume <end_msg_id>            — continues from where /fwd last left off
#   /fwdstatus                         — show source/target/progress
#   /fwdcancel                         — stop the running forward job
#   /fwd_settings                      — dashboard: source/target/progress/login
#                                         status + caption/button/filters, all
#                                         in one inline-button panel
#   /fwd_caption <text> | off          — prefix prepended to each forwarded
#                                         message's caption/text (off to clear)
#   /fwd_button <text> | <url> | off   — inline URL button attached to every
#                                         forwarded message (off to clear).
#                                         Also accepts fwdbot's regex button
#                                         syntax for multiple buttons/rows:
#                                         [Text][buttonurl:url] and
#                                         [Text][buttonurl:url:same] to keep
#                                         a button on the same row as the
#                                         previous one (ported from
#                                         fwdbot's BTN_URL_REGEX/parse_buttons).
#   /fwd_filter <types/exts,...> | off — skip message types (photo, video,
#                                         document, audio, voice, sticker,
#                                         animation, text) and/or file
#                                         extensions (.pdf, .zip, ...) while
#                                         forwarding (off to clear)

import asyncio
import os
import re
from pyrogram import Client, filters, enums
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, RPCError
from config import API_ID, API_HASH
from database.db import db

# Regex-based button syntax, ported from fwdbot's plugins/test.py (BTN_URL_REGEX /
# parse_buttons). Lets a button be written as [Text][buttonurl:https://...] and
# supports multiple buttons: put :same at the end of a pair to keep it on the
# same row as the one before it, otherwise it starts a new row.
#   [Join][buttonurl:https://t.me/chan]
#   [A][buttonurl:https://a.com][B][buttonurl:https://b.com:same]   -> A,B same row
BTN_URL_REGEX = re.compile(r"(\[([^\[]+?)]\[buttonurl:/{0,2}(.+?)(:same)?])")


def parse_buttons(text, markup=True):
    """Extract [Text][buttonurl:url] / [Text][buttonurl:url:same] pairs from
    text and build an InlineKeyboardMarkup (or the raw button rows if
    markup=False). Returns None if nothing matched."""
    if not text:
        return None
    buttons = []
    for match in BTN_URL_REGEX.finditer(text):
        n_escapes = 0
        to_check = match.start(1) - 1
        while to_check > 0 and text[to_check] == "\\":
            n_escapes += 1
            to_check -= 1
        if n_escapes % 2 == 0:
            btn_text, btn_url, same_row = match.group(2), match.group(3).strip(), bool(match.group(4))
            try:
                button = InlineKeyboardButton(btn_text, url=btn_url)
            except Exception:
                continue
            if same_row and buttons:
                buttons[-1].append(button)
            else:
                buttons.append([button])
    if not buttons:
        return None
    return InlineKeyboardMarkup(buttons) if markup else buttons


def strip_buttons(text):
    """Removes [Text][buttonurl:url] markup from a caption so it isn't shown
    as literal text once the button has been extracted from it."""
    if not text:
        return text
    return BTN_URL_REGEX.sub("", text).strip()

from Akbots.direct_utils import E_CHECK, E_CROSS, E_INFO, E_BOLT, E_ROCKET
from Akbots import task_manager
from Akbots import titanium

E_ARROW = '➜'
E_LOCK = '🔒'
E_PENCIL = '✏️'
E_BUTTON = '🔘'
E_FILTER = '🚫'
E_TRASH = '🗑'
MAX_RANGE = 5000          # hard cap per /fwd call, matches fwdbot-style batch limits
DELAY_SECS = 1.2          # gap between copies — keeps well under Bot API flood limits
PROGRESS_EVERY = 15       # edit status message every N messages
SAVE_EVERY = 5            # persist resume-checkpoint every N messages

# Recognised /fwd_filter tokens for message *type* (as opposed to a file
# extension, which is any token starting with "."). Kept as a set so
# _should_skip() can validate/match in O(1).
FILTER_TYPES = {"text", "photo", "video", "document", "audio", "voice", "sticker", "animation"}

# Extra /fwd_filter token kinds, ported from fwdbot's keyword/size skip
# support: "kw:word" skips any message whose text/caption contains that
# word (case-insensitive), and "maxsize:100MB" / "minsize:10MB" skip media
# above/below that size. Kept separate from FILTER_TYPES since they carry
# a value rather than being a fixed token.
import re as _re_filter
_SIZE_TOKEN_RE = _re_filter.compile(r"^(maxsize|minsize):(\d+(?:\.\d+)?)(b|kb|mb|gb)$")
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}


def _parse_size_token(token: str):
    """Returns (kind, bytes) for a 'maxsize:100MB' / 'minsize:10MB' token,
    or None if it doesn't match that syntax."""
    m = _SIZE_TOKEN_RE.match(token)
    if not m:
        return None
    kind, num, unit = m.groups()
    return kind, int(float(num) * _SIZE_UNITS[unit])


def _is_valid_filter_token(t: str) -> bool:
    return t in FILTER_TYPES or t.startswith(".") or t.startswith("kw:") or _parse_size_token(t) is not None


class _SkipMessage(Exception):
    """Raised internally by _copy_with_extras() to signal 'count this as
    skipped and move on' — e.g. a filtered message type, or a message id
    that no longer exists. Never leaves _forward_loop()."""
    pass


# user_id -> asyncio.Task, so /fwdcancel can stop just the forward job
# (separate from task_manager's global /cancel_all, though it's registered
# there too for visibility/consistency with the rest of the bot).
_RUNNING = {}


def _parse_chat(raw: str):
    raw = raw.strip()
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw if raw.startswith("@") else f"@{raw}"


async def _make_userbot(user_id: int):
    """Spins up a connected Client from the person's stored /login session
    string, or returns None if they haven't logged in / it's expired.
    Caller owns the connection and must disconnect() it when done."""
    session_str = await db.get_session(user_id)
    if not session_str:
        return None
    acc = Client(
        f"fwd_userbot_{user_id}",
        session_string=session_str,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        max_concurrent_transmissions=10,
    )
    try:
        await acc.connect()
        return acc
    except Exception:
        return None


async def _resolve_chat(bot_client: Client, user_id: int, chat_ref):
    """Tries the bot client first, then the person's userbot session.
    Returns (chat, via, userbot_or_None). userbot is left connected if it
    was the one that worked, so the caller can reuse/disconnect it."""
    try:
        chat = await bot_client.get_chat(chat_ref)
        return chat, "bot", None
    except RPCError:
        pass

    acc = await _make_userbot(user_id)
    if acc is None:
        return None, None, None
    try:
        chat = await acc.get_chat(chat_ref)
        return chat, "user", acc
    except RPCError:
        await acc.disconnect()
        return None, None, None




async def _resolve_and_store(client: Client, message: Message, which: str):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/set{which} -1001234567890</code> or <code>/set{which} @channelusername</code>\n"
            f"<i>Tries the bot first; if the bot can't access it, falls back to your /fwd_login session.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    chat_ref = _parse_chat(message.command[1])
    chat, via, acc = await _resolve_chat(client, user_id, chat_ref)
    if acc:
        await acc.disconnect()

    if not chat:
        has_session = bool(await db.get_session(user_id))
        hint = (
            "Your login session can't see it either — double check the chat id/username."
            if has_session else
            f"The bot isn't in that chat, and you haven't run /fwd_login yet — do that if it's a "
            f"private chat, so your own account can be used instead."
        )
        return await message.reply_text(
            f"<b>{E_CROSS} Can't access that chat.</b> {hint}",
            parse_mode=enums.ParseMode.HTML
        )

    if which == "source":
        await db.set_fwd_source(user_id, chat.id, via)
    else:
        await db.set_fwd_target(user_id, chat.id, via)

    via_note = f" <i>(via {'your account' if via == 'user' else 'the bot'})</i>"
    await message.reply_text(
        f"<b>{E_CHECK} {which.capitalize()} set:</b> {chat.title or chat.first_name or chat.id} "
        f"(<code>{chat.id}</code>){via_note}",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("setsource"))
async def setsource_cmd(client: Client, message: Message):
    await _resolve_and_store(client, message, "source")


@Client.on_message(filters.private & filters.command("settarget"))
async def settarget_cmd(client: Client, message: Message):
    await _resolve_and_store(client, message, "target")


@Client.on_message(filters.private & filters.command("fwdstatus"))
async def fwdstatus_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    s = await db.get_fwd_settings(user_id)
    running = "🟢 running" if user_id in _RUNNING and not _RUNNING[user_id].done() else "⚪ idle"
    login_note = f"{E_CHECK} logged in" if s['has_login'] else f"{E_CROSS} not logged in — /fwd_login"
    await message.reply_text(
        f"<b>{E_INFO} Forward status</b>\n\n"
        f"<b>Source:</b> <code>{s['source'] or 'not set'}</code> <i>({s['source_via']})</i>\n"
        f"<b>Target:</b> <code>{s['target'] or 'not set'}</code> <i>({s['target_via']})</i>\n"
        f"<b>Last forwarded id:</b> <code>{s['last_id'] or '-'}</code>\n"
        f"<b>Login:</b> {login_note}\n"
        f"<b>Status:</b> {running}\n\n"
        f"<i>Use /fwd_settings for the full panel (caption, button, filters).</i>",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("fwdcancel"))
async def fwdcancel_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    task = _RUNNING.get(user_id)
    if not task or task.done():
        return await message.reply_text(f"<b>{E_INFO} No forward job is running.</b>", parse_mode=enums.ParseMode.HTML)
    task.cancel()
    await message.reply_text(f"<b>🚫 Stopping...</b> current message will finish, then it'll halt.", parse_mode=enums.ParseMode.HTML)


# --------------------------------------------------------------------------
# /reset — wipe all forward config (source/target/caption/button/filters),
# ported from fwdbot's confirm-then-reset flow.
# --------------------------------------------------------------------------

@Client.on_message(filters.private & filters.command("reset"))
async def reset_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    task = _RUNNING.get(user_id)
    if task and not task.done():
        return await message.reply_text(
            f"<b>{E_CROSS} A forward job is running.</b> Run /fwdcancel first, then /reset.",
            parse_mode=enums.ParseMode.HTML
        )
    confirm_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, reset", callback_data=f"fwdreset:yes:{user_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="fwdreset:no"),
    ]])
    await message.reply_text(
        f"<b>⚠️ Reset confirmation</b>\n\n"
        f"This will clear your <b>source</b>, <b>target</b>, <b>progress</b>, "
        f"<b>caption</b>, <b>button</b>, and <b>filters</b> for forwarding.\n"
        f"<i>Your /login session is not affected.</i>\n\n"
        f"Are you sure?",
        reply_markup=confirm_kb, parse_mode=enums.ParseMode.HTML
    )


@Client.on_callback_query(filters.regex(r"^fwdreset:"))
async def reset_confirm_cb(client: Client, callback_query: CallbackQuery):
    action = callback_query.data.split(":")
    if action[1] == "no":
        return await callback_query.message.edit_text(f"<b>{E_INFO} Reset cancelled.</b>", parse_mode=enums.ParseMode.HTML)
    owner_id = int(action[2])
    if callback_query.from_user.id != owner_id:
        return await callback_query.answer("This isn't your reset request.", show_alert=True)
    await db.reset_fwd_settings(owner_id)
    await callback_query.message.edit_text(
        f"<b>{E_CHECK} Forward settings reset.</b> Source, target, caption, button, and filters are all cleared.",
        parse_mode=enums.ParseMode.HTML
    )


# --------------------------------------------------------------------------
# Caption prefix / inline button / type-filters
# --------------------------------------------------------------------------

@Client.on_message(filters.private & filters.command("fwd_caption"))
async def fwd_caption_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        s = await db.get_fwd_settings(user_id)
        current = f"<code>{s['caption']}</code>" if s['caption'] else "<i>not set</i>"
        return await message.reply_text(
            f"<b>{E_PENCIL} Forward caption prefix</b>\n\n"
            f"<b>Current:</b> {current}\n\n"
            f"<b>Usage:</b> <code>/fwd_caption &lt;text&gt;</code>\n"
            f"<code>/fwd_caption off</code> — clear it\n\n"
            f"<i>Prepended to each forwarded message's caption/text (HTML formatting allowed). "
            f"The original caption/text is kept below it.</i>",
            parse_mode=enums.ParseMode.HTML
        )

    arg = message.text.split(" ", 1)[1].strip()
    if arg.lower() == "off":
        await db.clear_fwd_caption(user_id)
        return await message.reply_text(f"<b>{E_CHECK} Caption prefix cleared.</b>", parse_mode=enums.ParseMode.HTML)

    await db.set_fwd_caption(user_id, arg)
    await message.reply_text(
        f"<b>{E_CHECK} Caption prefix saved.</b>\n\n<b>Preview:</b>\n{arg}",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("fwd_button"))
async def fwd_button_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        s = await db.get_fwd_settings(user_id)
        current = _fwd_button_preview(s['button'])
        return await message.reply_text(
            f"<b>{E_BUTTON} Forward inline button</b>\n\n"
            f"<b>Current:</b> {current}\n\n"
            f"<b>Simple usage:</b> <code>/fwd_button &lt;text&gt; | &lt;url&gt;</code>\n"
            f"<b>Multi-button syntax (regex):</b> <code>[Text][buttonurl:https://...]</code>\n"
            f"Add <code>:same</code> before the closing bracket to keep the next "
            f"button on the same row: <code>[A][buttonurl:https://a.com][B][buttonurl:https://b.com:same]</code>\n"
            f"<code>/fwd_button off</code> — clear it\n\n"
            f"<i>Example: /fwd_button Join Channel | https://t.me/yourchannel</i>\n"
            f"<i>Example: /fwd_button [Join][buttonurl:https://t.me/a][Website][buttonurl:https://x.com:same]</i>",
            parse_mode=enums.ParseMode.HTML
        )

    arg = message.text.split(" ", 1)[1].strip()
    if arg.lower() == "off":
        await db.clear_fwd_button(user_id)
        return await message.reply_text(f"<b>{E_CHECK} Button cleared.</b>", parse_mode=enums.ParseMode.HTML)

    # New syntax: [Text][buttonurl:url] (regex-parsed, supports multiple buttons/rows)
    if "buttonurl:" in arg and BTN_URL_REGEX.search(arg):
        parsed = parse_buttons(arg)
        if not parsed:
            return await message.reply_text(
                f"<b>{E_CROSS} Couldn't parse that button syntax.</b>\n"
                f"Format: <code>[Text][buttonurl:https://...]</code>",
                parse_mode=enums.ParseMode.HTML
            )
        await db.set_fwd_button(user_id, arg)
        return await message.reply_text(
            f"<b>{E_CHECK} Button(s) saved.</b>\n{_fwd_button_preview(arg)}",
            parse_mode=enums.ParseMode.HTML
        )

    # Legacy syntax: text | url  (single button, kept for backward compatibility)
    if "|" not in arg:
        return await message.reply_text(
            f"<b>{E_CROSS} Usage:</b> <code>/fwd_button &lt;text&gt; | &lt;url&gt;</code> "
            f"or <code>[Text][buttonurl:https://...]</code>",
            parse_mode=enums.ParseMode.HTML
        )

    text, url = (p.strip() for p in arg.split("|", 1))
    if not text or not url.startswith(("http://", "https://", "tg://")):
        return await message.reply_text(
            f"<b>{E_CROSS} Invalid button.</b> URL must start with http://, https:// or tg://",
            parse_mode=enums.ParseMode.HTML
        )

    # Round-trip it through pyrogram's own button object so a malformed url
    # (that passes the scheme check above but Telegram itself would still
    # reject) surfaces here instead of failing mid-forward-job later.
    try:
        InlineKeyboardButton(text, url=url)
    except Exception:
        return await message.reply_text(f"<b>{E_CROSS} Telegram rejected that button.</b> Check the text/url.", parse_mode=enums.ParseMode.HTML)

    await db.set_fwd_button(user_id, text, url)
    await message.reply_text(
        f"<b>{E_CHECK} Button saved:</b> <code>{text}</code> → {url}",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("fwd_filter"))
async def fwd_filter_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if len(message.command) < 2:
        s = await db.get_fwd_settings(user_id)
        current = ", ".join(f"<code>{f}</code>" for f in s['filters']) if s['filters'] else "<i>none</i>"
        return await message.reply_text(
            f"<b>{E_FILTER} Forward filters (skip list)</b>\n\n"
            f"<b>Current:</b> {current}\n\n"
            f"<b>Usage:</b> <code>/fwd_filter &lt;items,comma,separated&gt;</code>\n"
            f"<code>/fwd_filter off</code> — clear all\n\n"
            f"<b>Types:</b> <code>{', '.join(sorted(FILTER_TYPES))}</code>\n"
            f"<b>Extensions:</b> any token starting with a dot, e.g. <code>.pdf</code>, <code>.zip</code>\n"
            f"<b>Keywords:</b> <code>kw:word</code> — skips text/caption containing that word\n"
            f"<b>Size:</b> <code>maxsize:100MB</code> / <code>minsize:10MB</code> — skips media outside that size\n\n"
            f"<i>Example: /fwd_filter sticker,.exe,kw:sale,maxsize:200MB</i>",
            parse_mode=enums.ParseMode.HTML
        )

    arg = message.text.split(" ", 1)[1].strip()
    if arg.lower() == "off":
        await db.clear_fwd_filters(user_id)
        return await message.reply_text(f"<b>{E_CHECK} Filters cleared.</b>", parse_mode=enums.ParseMode.HTML)

    raw_items = [t.strip() for t in arg.split(",") if t.strip()]
    norm_items = []
    for t in raw_items:
        low = t.lower()
        if low.startswith("kw:"):
            norm_items.append("kw:" + t.split(":", 1)[1].strip().lower())
        elif low.startswith(("maxsize:", "minsize:")):
            norm_items.append(low.replace(" ", ""))
        else:
            norm_items.append(low)
    bad = [t for t in norm_items if not _is_valid_filter_token(t)]
    if bad:
        return await message.reply_text(
            f"<b>{E_CROSS} Unrecognised:</b> <code>{', '.join(bad)}</code>\n"
            f"<i>Use a type, a .extension, kw:word, or maxsize:/minsize: with a number+unit (b/kb/mb/gb).</i>",
            parse_mode=enums.ParseMode.HTML
        )

    await db.set_fwd_filters(user_id, norm_items)
    await message.reply_text(
        f"<b>{E_CHECK} Filters saved:</b> <code>{', '.join(norm_items)}</code>\n"
        f"<i>Matching messages will be skipped during forwarding.</i>",
        parse_mode=enums.ParseMode.HTML
    )


# --------------------------------------------------------------------------
# /fwd_settings — one dashboard for everything above
# --------------------------------------------------------------------------

def _fwd_button_preview(button) -> str:
    """button may be: None, the legacy {'text','url'} dict, or the new raw
    [Text][buttonurl:url] string. Returns a short human-readable preview."""
    if not button:
        return "<i>not set</i>"
    if isinstance(button, dict):
        return f"<code>{button['text']}</code> → {button['url']}"
    rows = parse_buttons(button, markup=False) or []
    if not rows:
        return f"<code>{button}</code>"
    parts = [btn.text for row in rows for btn in row]
    return " | ".join(f"<code>{p}</code>" for p in parts)


def _settings_text(user_id: int, s: dict) -> str:
    running = "🟢 running" if user_id in _RUNNING and not _RUNNING[user_id].done() else "⚪ idle"
    login_note = f"{E_CHECK} logged in" if s['has_login'] else f"{E_CROSS} not logged in"
    caption_note = "set ✔️" if s['caption'] else "not set"
    button_note = _fwd_button_preview(s['button'])
    filters_note = ", ".join(s['filters']) if s['filters'] else "none"
    return (
        f"<b>{E_INFO} Forward Settings</b>\n\n"
        f"<b>Source:</b> <code>{s['source'] or 'not set'}</code> <i>({s['source_via']})</i>\n"
        f"<b>Target:</b> <code>{s['target'] or 'not set'}</code> <i>({s['target_via']})</i>\n"
        f"<b>Progress:</b> <code>{s['last_id'] or '-'}</code>\n"
        f"<b>Login:</b> {login_note}\n"
        f"<b>Job status:</b> {running}\n\n"
        f"<b>{E_PENCIL} Caption prefix:</b> {caption_note}\n"
        f"<b>{E_BUTTON} Button:</b> {button_note}\n"
        f"<b>{E_FILTER} Filters:</b> {filters_note}\n\n"
        f"<i>Tap a button below to view/change that setting.</i>"
    )


def _settings_buttons() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔀 Source", callback_data="fwds:source"),
            InlineKeyboardButton("🎯 Target", callback_data="fwds:target"),
        ],
        [
            InlineKeyboardButton(f"{E_PENCIL} Caption", callback_data="fwds:caption"),
            InlineKeyboardButton(f"{E_BUTTON} Button", callback_data="fwds:button"),
        ],
        [
            InlineKeyboardButton(f"{E_FILTER} Filters", callback_data="fwds:filters"),
            InlineKeyboardButton(f"{E_TRASH} Reset Progress", callback_data="fwds:resetprogress"),
        ],
        [InlineKeyboardButton("🔄 Refresh", callback_data="fwds:back"), InlineKeyboardButton("❌ Close", callback_data="fwds:close")],
    ])


@Client.on_message(filters.private & filters.command("fwd_settings"))
async def fwd_settings_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)
    s = await db.get_fwd_settings(user_id)
    await message.reply_text(_settings_text(user_id, s), reply_markup=_settings_buttons(), parse_mode=enums.ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^fwds:(source|target|caption|button|filters|resetprogress|back|close)$"))
async def fwd_settings_callbacks(client: Client, callback_query: CallbackQuery):
    action = callback_query.matches[0].group(1)
    user_id = callback_query.from_user.id

    if action == "close":
        await callback_query.message.delete()
        return await callback_query.answer()

    if action == "back":
        s = await db.get_fwd_settings(user_id)
        await callback_query.edit_message_text(_settings_text(user_id, s), reply_markup=_settings_buttons(), parse_mode=enums.ParseMode.HTML)
        return await callback_query.answer("Refreshed")

    if action == "resetprogress":
        await db.clear_fwd_progress(user_id)
        s = await db.get_fwd_settings(user_id)
        await callback_query.edit_message_text(_settings_text(user_id, s), reply_markup=_settings_buttons(), parse_mode=enums.ParseMode.HTML)
        return await callback_query.answer(f"{E_CHECK} Progress reset.")

    back_btn = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="fwds:back")]])

    if action in ("source", "target"):
        s = await db.get_fwd_settings(user_id)
        current = s['source'] if action == "source" else s['target']
        via = s['source_via'] if action == "source" else s['target_via']
        text = (
            f"<b>{'🔀 Source' if action == 'source' else '🎯 Target'} chat</b>\n\n"
            f"<b>Current:</b> <code>{current or 'not set'}</code> <i>({via})</i>\n\n"
            f"<b>Usage:</b> <code>/set{action} &lt;chat_id or @username&gt;</code>\n\n"
            f"<i>Send that command anywhere in this chat — this panel doesn't take text input directly.</i>"
        )
    elif action == "caption":
        s = await db.get_fwd_settings(user_id)
        current = f"<code>{s['caption']}</code>" if s['caption'] else "<i>not set</i>"
        text = (
            f"<b>{E_PENCIL} Caption prefix</b>\n\n"
            f"<b>Current:</b> {current}\n\n"
            f"<b>Usage:</b> <code>/fwd_caption &lt;text&gt;</code> or <code>/fwd_caption off</code>"
        )
    elif action == "button":
        s = await db.get_fwd_settings(user_id)
        current = _fwd_button_preview(s['button'])
        text = (
            f"<b>{E_BUTTON} Inline button</b>\n\n"
            f"<b>Current:</b> {current}\n\n"
            f"<b>Usage:</b> <code>/fwd_button &lt;text&gt; | &lt;url&gt;</code> or <code>/fwd_button off</code>"
        )
    else:  # filters
        s = await db.get_fwd_settings(user_id)
        current = ", ".join(f"<code>{f}</code>" for f in s['filters']) if s['filters'] else "<i>none</i>"
        text = (
            f"<b>{E_FILTER} Filters (skip list)</b>\n\n"
            f"<b>Current:</b> {current}\n\n"
            f"<b>Usage:</b> <code>/fwd_filter &lt;items,comma,separated&gt;</code> or <code>/fwd_filter off</code>\n"
            f"<b>Types:</b> <code>{', '.join(sorted(FILTER_TYPES))}</code>\n"
            f"<b>Extensions:</b> dot-prefixed, e.g. <code>.pdf</code>"
        )

    await callback_query.edit_message_text(text, reply_markup=back_btn, parse_mode=enums.ParseMode.HTML)
    await callback_query.answer()


async def _pick_job_client(bot_client: Client, user_id: int, source, target, source_via, target_via):
    """Works out ONE client that can both read `source` and write `target`
    for this run. Returns (client, owns_it) — owns_it=True means the caller
    must disconnect() it when the job ends (it's a fresh userbot client);
    False means it's the shared bot client, leave it alone."""
    if source_via == "bot" and target_via == "bot":
        job_client, is_clone, clone_username = await titanium.get_job_client(user_id, bot_client, source, target)
        return job_client, False  # clone bots live in titanium._CLONE_CACHE and are reused — never disconnect here

    # Either side needs the personal account — the same account has to be
    # able to reach both, since copy_message runs on a single client.
    acc = await _make_userbot(user_id)
    if acc is None:
        return None, False
    try:
        await acc.get_chat(source)
        await acc.get_chat(target)
        return acc, True
    except RPCError:
        await acc.disconnect()
        return None, False


def _build_button_markup(button):
    """button is whatever db.get_fwd_settings() returns for 'button': either
    the legacy {'text':..., 'url':...} dict (old installs), the new raw
    [Text][buttonurl:url] string (supports multiple buttons/rows via
    :same), or None. Returns an InlineKeyboardMarkup, or None if nothing's
    set — wrapped in try/except since this runs at job-launch time against
    whatever's in the DB."""
    if not button:
        return None
    try:
        if isinstance(button, dict):
            return InlineKeyboardMarkup([[InlineKeyboardButton(button["text"], url=button["url"])]])
        return parse_buttons(button)
    except Exception:
        return None


def _msg_type_and_ext(msg: Message):
    """Returns (type_name, extension_or_None) for filter matching. extension
    includes the leading dot and is lowercased, e.g. '.pdf'."""
    for attr, name in (
        ("photo", "photo"), ("video", "video"), ("document", "document"),
        ("audio", "audio"), ("voice", "voice"), ("sticker", "sticker"),
        ("animation", "animation"),
    ):
        obj = getattr(msg, attr, None)
        if obj:
            fname = getattr(obj, "file_name", None)
            ext = os.path.splitext(fname)[1].lower() if fname else None
            return name, ext
    if msg.text:
        return "text", None
    return "other", None


def _msg_size_bytes(msg: Message):
    """Returns the byte size of the media in msg, or None for text/no media."""
    for attr in ("document", "video", "audio", "voice", "animation", "photo"):
        obj = getattr(msg, attr, None)
        if obj:
            return getattr(obj, "file_size", None)
    return None


def _should_skip(msg: Message, filters_set: set) -> bool:
    if not filters_set:
        return False
    mtype, ext = _msg_type_and_ext(msg)
    if mtype in filters_set:
        return True
    if ext and ext in filters_set:
        return True

    haystack = None
    size = None
    for item in filters_set:
        if item.startswith("kw:"):
            if haystack is None:
                haystack = ((msg.text or "") + " " + (msg.caption or "")).lower()
            if item[3:] in haystack:
                return True
        elif item.startswith(("maxsize:", "minsize:")):
            parsed = _parse_size_token(item)
            if not parsed:
                continue
            if size is None:
                size = _msg_size_bytes(msg)
            if size is None:
                continue
            kind, limit = parsed
            if kind == "maxsize" and size > limit:
                return True
            if kind == "minsize" and size < limit:
                return True
    return False


async def _copy_with_extras(job_client: Client, source, target, msg_id: int,
                             caption_prefix, button_markup, filters_set: set):
    """The slow path — only used when a caption prefix and/or filters are
    configured, since both need the original message fetched first (a
    caption prefix needs the original caption/text to prepend to; a filter
    needs to know the message's type). copy_message()'s own caption param
    is ignored for plain-text messages (Bot API only applies it to media),
    so text messages with a caption prefix are re-sent via send_message()
    instead of copy_message() — everything else still goes through
    copy_message() so restricted-forwarding chats keep working."""
    orig = await job_client.get_messages(source, msg_id)
    if orig is None or getattr(orig, "empty", False):
        raise _SkipMessage()

    if _should_skip(orig, filters_set):
        raise _SkipMessage()

    if not caption_prefix:
        await job_client.copy_message(chat_id=target, from_chat_id=source, message_id=msg_id, reply_markup=button_markup)
        return

    if orig.media:
        original_html = orig.caption.html if orig.caption else ""
        new_caption = f"{caption_prefix}\n\n{original_html}" if original_html else caption_prefix
        await job_client.copy_message(
            chat_id=target, from_chat_id=source, message_id=msg_id,
            caption=new_caption, parse_mode=enums.ParseMode.HTML, reply_markup=button_markup
        )
    elif orig.text:
        new_text = f"{caption_prefix}\n\n{orig.text.html}"
        await job_client.send_message(
            target, new_text, parse_mode=enums.ParseMode.HTML,
            reply_markup=button_markup, disable_web_page_preview=True
        )
    else:
        # Service message or something with neither text nor media —
        # nothing to prefix, just copy it through as-is.
        await job_client.copy_message(chat_id=target, from_chat_id=source, message_id=msg_id, reply_markup=button_markup)


async def _forward_loop(job_client: Client, owns_client: bool, message: Message, status: Message, user_id: int,
                         source, target, start_id: int, end_id: int,
                         caption_prefix=None, button_markup=None, filters_set=None):
    filters_set = filters_set or set()
    need_extras = bool(caption_prefix) or bool(filters_set)
    done = skipped = failed = 0
    total = end_id - start_id + 1
    msg_id = start_id
    try:
        while msg_id <= end_id:
            try:
                if need_extras:
                    await _copy_with_extras(job_client, source, target, msg_id, caption_prefix, button_markup, filters_set)
                else:
                    await job_client.copy_message(chat_id=target, from_chat_id=source, message_id=msg_id, reply_markup=button_markup)
                done += 1
            except _SkipMessage:
                skipped += 1
            except FloodWait as e:
                await asyncio.sleep(e.value + 1)
                continue  # retry same msg_id
            except RPCError:
                skipped += 1  # deleted / service message / no access to that one message
            except Exception:
                failed += 1

            if msg_id % SAVE_EVERY == 0:
                await db.set_fwd_progress(user_id, msg_id, {"source": source, "target": target})

            if (done + skipped + failed) % PROGRESS_EVERY == 0:
                processed = done + skipped + failed
                pct = processed * 100 // total
                await status.edit_text(
                    f"<b>{E_ROCKET} Forwarding...</b> {pct}%\n"
                    f"<code>{processed}/{total}</code> | ✅ {done} | ⏭ {skipped} | ❌ {failed}\n"
                    f"<i>Currently at id {msg_id}</i>",
                    parse_mode=enums.ParseMode.HTML
                )

            msg_id += 1
            await asyncio.sleep(DELAY_SECS)

        await db.set_fwd_progress(user_id, end_id, {"source": source, "target": target})
        await status.edit_text(
            f"<b>{E_CHECK} Forward complete.</b>\n"
            f"<b>Range:</b> <code>{start_id}-{end_id}</code>\n"
            f"✅ {done} sent | ⏭ {skipped} skipped | ❌ {failed} failed",
            parse_mode=enums.ParseMode.HTML
        )
    except asyncio.CancelledError:
        await db.set_fwd_progress(user_id, msg_id - 1, {"source": source, "target": target})
        await status.edit_text(
            f"<b>🚫 Forward stopped at id {msg_id - 1}.</b>\n"
            f"✅ {done} sent | ⏭ {skipped} skipped | ❌ {failed} failed\n"
            f"<i>Resume anytime with /fwdresume {end_id}</i>",
            parse_mode=enums.ParseMode.HTML
        )
        raise
    finally:
        _RUNNING.pop(user_id, None)
        if owns_client:
            try:
                await job_client.disconnect()
            except Exception:
                pass


async def _launch(client: Client, message: Message, start_id: int, end_id: int):
    user_id = message.from_user.id
    s = await db.get_fwd_settings(user_id)
    if not s["source"] or not s["target"]:
        return await message.reply_text(
            f"<b>{E_INFO} Set both first:</b> <code>/setsource</code> and <code>/settarget</code>.",
            parse_mode=enums.ParseMode.HTML
        )
    if user_id in _RUNNING and not _RUNNING[user_id].done():
        return await message.reply_text(f"<b>{E_INFO} A forward job is already running.</b> Use /fwdcancel to stop it first.", parse_mode=enums.ParseMode.HTML)
    if start_id > end_id:
        return await message.reply_text(f"<b>{E_CROSS} Start id must be ≤ end id.</b>", parse_mode=enums.ParseMode.HTML)
    if end_id - start_id + 1 > MAX_RANGE:
        return await message.reply_text(f"<b>{E_CROSS} Range too big — max {MAX_RANGE} messages per run.</b> Split it up.", parse_mode=enums.ParseMode.HTML)

    status = await message.reply_text(f"<b>{E_BOLT} Starting forward job...</b>", parse_mode=enums.ParseMode.HTML)

    job_client, owns_client = await _pick_job_client(
        client, user_id, s["source"], s["target"], s["source_via"], s["target_via"]
    )
    if job_client is None:
        via_needed = "your /login account" if "user" in (s["source_via"], s["target_via"]) else "the bot"
        return await status.edit_text(
            f"<b>{E_CROSS} Can't run this job.</b> {via_needed} can't reach both the source and "
            f"target chat at once — since source and target were set through different access "
            f"methods, no single connection can do the copy. Re-run /setsource and /settarget so "
            f"both resolve the same way (both via the bot, or both via /login).",
            parse_mode=enums.ParseMode.HTML
        )

    task = asyncio.ensure_future(_forward_loop(
        job_client, owns_client, message, status, user_id, s["source"], s["target"], start_id, end_id,
        caption_prefix=s.get("caption"),
        button_markup=_build_button_markup(s.get("button")),
        filters_set=set(s.get("filters") or []),
    ))
    _RUNNING[user_id] = task
    task_id = task_manager.register(user_id, task, f"Forward {start_id}-{end_id}")
    task.add_done_callback(lambda t: task_manager.unregister(user_id, task_id))


@Client.on_message(filters.private & filters.command(["fwd", "forward"]))
async def fwd_cmd(client: Client, message: Message):
    if len(message.command) < 3 or not all(p.lstrip("-").isdigit() for p in message.command[1:3]):
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/fwd &lt;start_msg_id&gt; &lt;end_msg_id&gt;</code>\n"
            f"<i>Message id = the number you see when you copy a message link, e.g. "
            f"t.me/c/12345/<b>678</b> → id is 678.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    await _launch(client, message, int(message.command[1]), int(message.command[2]))


@Client.on_message(filters.private & filters.command("fwdresume"))
async def fwdresume_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    s = await db.get_fwd_settings(user_id)
    if not s["last_id"]:
        return await message.reply_text(f"<b>{E_INFO} Nothing to resume.</b> Use /fwd to start a fresh job.", parse_mode=enums.ParseMode.HTML)
    if len(message.command) < 2 or not message.command[1].lstrip("-").isdigit():
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/fwdresume &lt;end_msg_id&gt;</code>", parse_mode=enums.ParseMode.HTML)
    await _launch(client, message, int(s["last_id"]) + 1, int(message.command[1]))
