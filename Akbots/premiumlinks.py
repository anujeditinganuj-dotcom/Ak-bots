# Akbots - Don't Remove Credit - @AkBots_Official
#
# Telegram-facing wrapper around Akbots/bypassers/{filepress,gdflix,hubcloud}.py.
# Those three files only SCRAPE a premium-link page and return a dict of
# extracted mirror links (Telegram bot link, Google Drive direct link,
# GoFile, PixelDrain, cloud-resume, zfile workers.dev links) — they don't
# talk to Telegram or download anything themselves. This file is what
# actually turns "paste a link" into "video shows up in the chat": pick the
# best directly-fetchable link out of that dict, then reuse the same
# download + thumbnail/duration + upload pipeline every other plugin here
# uses (Akbots/direct_utils.py).

import os
import re
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Akbots.bypassers.filepress import async_scrape_filepress
from Akbots.bypassers.gdflix import async_scrape_gdflix
from Akbots.bypassers.hubcloud import async_scrape_hubcloud
from Akbots.direct_utils import (
    make_output_folder, safe_filename, stream_download, upload_file,
    E_CROSS, E_INFO, E_ROCKET,
)

OUTPUT_FOLDER = make_output_folder("premiumlinks")

# These sites are almost always run on ever-changing clone domains (that's
# the whole point — the "real" domain gets blocked, a new one pops up), so
# a domain allowlist can never be exhaustive. Auto-detect covers the common/
# well-known base names; anything on a fresh clone domain still works via
# the explicit /filepress, /gdflix, /hubcloud commands.
FILEPRESS_PATTERN = re.compile(
    r"(https?://)?[\w.\-]*(filepress|hubdrive|gdlink|gdtot|gdflix\.top)[\w.\-]*/file/[A-Za-z0-9]+",
    re.IGNORECASE,
)
GDFLIX_PATTERN = re.compile(
    r"(https?://)?[\w.\-]*gdflix[\w.\-]*/(file|zfile)/[A-Za-z0-9]+",
    re.IGNORECASE,
)
HUBCLOUD_PATTERN = re.compile(
    r"(https?://)?[\w.\-]*hubcloud[\w.\-]*/(drive|video|packs)/[A-Za-z0-9]+"
    r"|(https?://)?vifix\.site/hubcloud/\S+",
    re.IGNORECASE,
)

# Domain keywords these auto-detect patterns are built from — exported so
# urluploader.py's generic-link fallback can exclude them (same pattern
# already used for TERABOX_DOMAINS), otherwise a pasted link would get
# processed twice: once here, once as a raw file by urluploader.py.
BYPASS_DOMAINS = ("filepress", "hubdrive", "gdlink", "gdtot", "gdflix", "hubcloud", "vifix.site")


def _href(link_html) -> str | None:
    """The scrapers wrap every link as '<a href="...">𝗟𝗜𝗡𝗞</a>' — pull the
    raw URL back out. Returns None for empty/missing entries."""
    if not link_html:
        return None
    m = re.search(r'href="([^"]+)"', str(link_html))
    if m:
        return m.group(1)
    return link_html if str(link_html).startswith("http") else None


def _pick_best_link(data: dict) -> str | None:
    """Priority order favors links that are a plain HTTP GET away from the
    actual file bytes. Telegram bot links (need /start with another bot)
    and GoFile page links (need GoFile's own API/token, not a raw file URL)
    are deliberately skipped here — they're still shown to the user as a
    fallback if nothing better is found."""
    for key in ("instantdl", "cloud_resume"):
        href = _href(data.get(key))
        if href:
            return href
    for item in data.get("zfile") or []:
        href = _href(item)
        if href:
            return href
    href = _href(data.get("pixeldrain"))
    if href:
        return href
    return None


def _format_links_summary(data: dict, service_label: str) -> str:
    lines = [f"<b>{E_INFO} {service_label}: {data.get('title', 'Unknown')}</b>"]
    if data.get("size"):
        lines.append(f"📦 Size: {data['size']}")
    for key, label in (
        ("instantdl", "⚡ Instant"), ("cloud_resume", "☁️ Cloud Resume"),
        ("telegram", "✈️ Telegram"), ("gofile", "📁 GoFile"),
        ("pixeldrain", "💧 PixelDrain"),
    ):
        if data.get(key):
            lines.append(f"{label}: {data[key]}")
    for i, z in enumerate(data.get("zfile") or [], start=1):
        lines.append(f"🗂️ Mirror {i}: {z}")
    return "\n".join(lines)


async def _run_bypass_download(client: Client, message: Message, url: str, scraper_fn, service_label: str):
    status = await message.reply_text(
        f"<b>{E_INFO} {service_label} link detected — extracting...</b>", parse_mode=enums.ParseMode.HTML
    )

    try:
        data = await scraper_fn(url)
    except Exception as e:
        return await status.edit_text(
            f"<b>{E_CROSS} Extraction failed:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML
        )

    if not data:
        return await status.edit_text(
            f"<b>{E_CROSS} Could not extract anything from this link.</b>\n"
            f"It may be dead, region-locked, or the site changed its layout.",
            parse_mode=enums.ParseMode.HTML,
        )

    if data.get("is_pack"):
        # A season/series pack is a page full of per-episode links, each of
        # which needs its own separate bypass pass — auto-downloading an
        # entire pack in one go is out of scope, so hand back the episode
        # links instead of guessing which one the user wants.
        text = (
            f"<b>{E_INFO} {service_label} Pack: {data.get('title', 'Unknown')}</b>\n"
            f"📦 Size: {data.get('size', 'Unknown')}\n\n"
            f"{data.get('pack_content', 'No episodes found.')}\n\n"
            f"<i>Send me one episode link at a time to download it.</i>"
        )
        return await status.edit_text(text, parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True)

    best = _pick_best_link(data)
    if not best:
        return await status.edit_text(
            _format_links_summary(data, service_label)
            + f"\n\n<i>{E_INFO} No directly-downloadable link found — try one of the links above manually.</i>",
            parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True,
        )

    title = data.get("title") or "file"
    filename = safe_filename(f"{title}.mp4", "premiumlink_file")
    dest = os.path.join(OUTPUT_FOLDER, f"{message.id}_{filename}")

    try:
        await status.edit_text(f"<b>{E_ROCKET} Downloading: {title}</b>", parse_mode=enums.ParseMode.HTML)
        await stream_download(
            best, dest, status, f"Downloading from {service_label}",
            user_id=message.from_user.id, file_name=filename,
        )
        await upload_file(client, message, dest, status, _format_links_summary(data, service_label), file_name=filename)
    except Exception as e:
        try:
            await status.edit_text(
                _format_links_summary(data, service_label)
                + f"\n\n<b>{E_CROSS} Auto-download failed:</b>\n<code>{e}</code>\n"
                  f"<i>Try one of the links above manually.</i>",
                parse_mode=enums.ParseMode.HTML, disable_web_page_preview=True,
            )
        except Exception:
            pass
    finally:
        try:
            os.remove(dest)
        except Exception:
            pass


def _extract(pattern: re.Pattern, text: str):
    m = pattern.search(text)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Commands (work for ANY clone domain, since the URL is given explicitly)
# ---------------------------------------------------------------------------

@Client.on_message(filters.command(["filepress", "hubdrive"]) & filters.private)
async def filepress_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/filepress &lt;url&gt;</code>", parse_mode=enums.ParseMode.HTML
        )
    await _run_bypass_download(client, message, message.command[1], async_scrape_filepress, "FilePress")


@Client.on_message(filters.command(["gdflix"]) & filters.private)
async def gdflix_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/gdflix &lt;url&gt;</code>", parse_mode=enums.ParseMode.HTML
        )
    await _run_bypass_download(client, message, message.command[1], async_scrape_gdflix, "GDFlix")


@Client.on_message(filters.command(["hubcloud"]) & filters.private)
async def hubcloud_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/hubcloud &lt;url&gt;</code>", parse_mode=enums.ParseMode.HTML
        )
    await _run_bypass_download(client, message, message.command[1], async_scrape_hubcloud, "HubCloud")


# ---------------------------------------------------------------------------
# Auto-detect (bare link, no command) — same group=1 priority as the other
# dedicated site handlers (facebook.py, instagram.py, terabox.py, vk.py).
# ---------------------------------------------------------------------------

@Client.on_message(filters.text & filters.private & filters.regex(FILEPRESS_PATTERN) & ~filters.regex(r"^/"), group=1)
async def filepress_auto_detect(client: Client, message: Message):
    url = _extract(FILEPRESS_PATTERN, message.text)
    if url:
        await _run_bypass_download(client, message, url, async_scrape_filepress, "FilePress")


@Client.on_message(filters.text & filters.private & filters.regex(GDFLIX_PATTERN) & ~filters.regex(r"^/"), group=1)
async def gdflix_auto_detect(client: Client, message: Message):
    url = _extract(GDFLIX_PATTERN, message.text)
    if url:
        await _run_bypass_download(client, message, url, async_scrape_gdflix, "GDFlix")


@Client.on_message(filters.text & filters.private & filters.regex(HUBCLOUD_PATTERN) & ~filters.regex(r"^/"), group=1)
async def hubcloud_auto_detect(client: Client, message: Message):
    url = _extract(HUBCLOUD_PATTERN, message.text)
    if url:
        await _run_bypass_download(client, message, url, async_scrape_hubcloud, "HubCloud")
