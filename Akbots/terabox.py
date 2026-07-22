import os
import re
import asyncio
import aiohttp
from urllib.parse import quote, unquote, urlparse, parse_qs
from pyrogram import Client, filters, enums
from pyrogram.types import Message

from Akbots.direct_utils import (
    make_output_folder, safe_filename, stream_download, upload_file,
    E_CHECK, E_CROSS, E_INFO
)
from Akbots.link_cache import try_send_cached

# Reuses the same headless-Chromium setup as Akbots/headless.py (no new
# browser/driver dependency) to render teradownloader.com, which — like the
# JS-rendered players headless.py handles — only builds its real download
# link client-side; the raw HTML is just a "Loading..." placeholder.
try:
    from playwright.async_api import async_playwright
    from Akbots.headless import _ensure_chromium, system_chromium_path
except ImportError:
    async_playwright = None
    _ensure_chromium = None
    system_chromium_path = None

# Single source of truth for every TeraBox / mirror domain this plugin
# handles. Akbots/urluploader.py (the generic last-resort uploader) imports
# this tuple to build its own exclusion list, so the two plugins can never
# drift out of sync again — previously urluploader.py hard-coded only the
# original 6 domains and re-processed every link on this longer list a
# second time as a "raw file" after terabox.py had already delivered it.
TERABOX_DOMAINS = (
    "terabox.com", "1024terabox.com", "teraboxapp.com", "freeterabox.com",
    "nephobox.com", "4funbox.com", "4funbox.co", "4funbox.in", "terabox.app", "terabox.fun",
    "1024tera.com", "1024tera.co", "1024-terabox.com", "tera1024box.com",
    "mirrobox.com", "momerybox.com", "tibibox.com",
    "dubox.com", "terafileshare.com", "terasharelink.com", "teraboxlink.com",
    "terabox.link", "teraboxurl.com", "teraboxshare.com", "teraboxfree.com",
    "teraboxsharefile.com", "terabox.club", "terabox.click",
    "terasharefile.com", "terashareus.com", "gibibox.com", "pebibox.com",
    "fancybox.in", "bestclouddrive.com",
)

PATTERN = re.compile(
    r"(https?://)?(www\.)?("
    + "|".join(re.escape(d) for d in TERABOX_DOMAINS)
    + r")/\S+",
    re.IGNORECASE
)

# NOTE: savetube.me (the previous extraction API) has been permanently
# retired. Two extraction methods now run in order:
#   1. A direct scrape of the TeraBox share page itself (no browser needed,
#      fast) — ported from teradownloader-main's server/services/
#      teraboxService.js (Node/cheerio) to aiohttp + regex below.
#   2. If that finds nothing (TeraBox's page markup shifts often), the
#      existing teradownloader.com + headless Chromium fallback further
#      down in this file kicks in.
# Running both means a change on either side (TeraBox's own site, or
# teradownloader.com) doesn't take the whole plugin down at once.

_TERADOWNLOADER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_DIRECT_SCRAPE_HEADERS_EXTRA = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Patterns for pulling a real download link out of a TeraBox share page's
# raw HTML/inline <script> content. Checked in order; first match wins.
_DIRECT_DLINK_PATTERNS = (
    re.compile(r'"dlink"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"downloadUrl"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"download_url"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'downloadUrl["\s]*:["\s]*["\']([^"\']+)["\']', re.IGNORECASE),
    re.compile(r'https?://[^"\'\s]*d\.terabox[^"\'\s]+', re.IGNORECASE),
    re.compile(r'https?://[^"\'\s]*terabox[^"\'\s]*/[^"\'\s]+\.(?:mp4|mp3|pdf|zip|rar|avi|mkv|mov|jpg|png|gif|doc|docx|txt|jpeg|webp)', re.IGNORECASE),
)

_DIRECT_FILENAME_PATTERNS = (
    re.compile(r'"filename"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"file_name"\s*:\s*"([^"]+)"', re.IGNORECASE),
    re.compile(r'"server_filename"\s*:\s*"([^"]+)"', re.IGNORECASE),
)


def _extract_filename_from_html(html: str, page_url: str) -> str | None:
    """Best-effort filename lookup straight from a TeraBox share page's
    HTML — mirrors extractFileName() in teraboxService.js (og:title meta
    tag, then inline JSON, then the URL itself), stripped down to what's
    reusable outside a browser DOM."""
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE)
    name = m.group(1) if m else None

    if not name or "terabox" in name.lower():
        for pat in _DIRECT_FILENAME_PATTERNS:
            m = pat.search(html)
            if m:
                name = m.group(1)
                break

    if not name:
        base = os.path.basename(urlparse(page_url).path)
        name = unquote(base) if base else None

    if name:
        name = re.sub(r"\s*-\s*Tera[Bb]ox.*$", "", name).strip()
        return safe_filename(name, "terabox_download")
    return None


def _extract_download_url_from_html(html: str) -> str | None:
    """Mirrors getDirectDownloadUrl()'s script-tag regex scan in
    teraboxService.js — TeraBox share pages often embed the eventual dlink
    (or enough of the page's own API JSON) directly in inline <script>
    content, avoiding the need to render the page in a real browser."""
    for pat in _DIRECT_DLINK_PATTERNS:
        m = pat.search(html)
        if m:
            candidate = m.group(1) if m.groups() else m.group(0)
            if candidate.startswith("http"):
                return candidate.encode().decode("unicode_escape") if "\\u" in candidate else candidate
    return None


async def _fetch_via_direct_scrape(link: str, timeout: int = 20):
    """Attempts to extract a direct download link straight from the
    TeraBox share page's own HTML/inline scripts — no browser required.
    Ported from teradownloader-main's teraboxService.js. Raises ValueError
    if no usable link is found, so the caller can fall back to the
    Playwright + teradownloader.com method.
    """
    parsed = urlparse(link)
    domain = parsed.netloc
    headers = {"User-Agent": _TERADOWNLOADER_UA, "Referer": f"https://{domain}/", **_DIRECT_SCRAPE_HEADERS_EXTRA}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.get(link, headers=headers, allow_redirects=True) as resp:
            html = await resp.text(errors="ignore")
            final_url = str(resp.url)

    download_url = _extract_download_url_from_html(html)
    if not download_url:
        raise ValueError("direct scrape: no dlink found in share page markup")

    if not await _head_ok(download_url):
        raise ValueError("direct scrape: candidate dlink did not resolve")

    filename = _extract_filename_from_html(html, final_url) or safe_filename(None, "terabox_file")
    return [(download_url, filename)]


def extract_url(text: str):
    m = PATTERN.search(text)
    return m.group(0) if m else None


# ── Guest-session extraction (no NDUS login needed at all) ─────────────────
# Ported from the Cloudflare Worker (tera_api_final-4.js's "Mode 1", itself
# ported from TeraDL's terabox1.py): for a PUBLIC share, TeraBox's mobile/wap
# endpoints (wap/share/filelist, api/shorturlinfo, share/download) work with
# a fresh, anonymous guest session — no logged-in NDUS cookie required at
# all (ours or any public pool's). This is a genuinely independent fallback:
# it doesn't depend on any account being valid/not-rate-limited, only on the
# share itself being public. Trade-off: no HLS streaming link this way, only
# a raw direct-file dlink — which is exactly what stream_download()/
# upload_file() below already use anyway, so nothing is lost for this bot.
GUEST_UA = ("Mozilla/5.0 (Linux; Android 6.0; Nexus 5 Build/MRA58N) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36")


def _resolve_surl(text: str) -> str | None:
    """Mirrors the Worker's resolveSurl(): pulls the share-id ('surl') out
    of a full TeraBox URL (either /s/<surl> path or a ?surl= query param),
    or passes a bare surl/ID straight through. The guest-session endpoints
    below talk to TeraBox by surl directly, not the full share URL."""
    text = text.strip()
    if text.startswith("http"):
        try:
            parsed = urlparse(text)
            m = re.search(r"/s/([a-zA-Z0-9_-]+)", parsed.path)
            if m:
                return m.group(1)
            q = parse_qs(parsed.query).get("surl")
            if q:
                return q[0]
        except Exception:
            pass
        return None
    if "/s/" in text:
        m = re.search(r"/s/([a-zA-Z0-9_-]+)", text)
        if m:
            return m.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]+", text):
        return text
    return None


def _extract_set_cookies(resp) -> str:
    """Mirrors the Worker's extractSetCookies(): joins every Set-Cookie
    response header's name=value part (dropping Path/Expires/etc.
    attributes) into one Cookie-header-ready string."""
    return "; ".join(v.split(";")[0] for v in resp.headers.getall("Set-Cookie", []))


async def _fetch_guest_session(surl: str):
    """Ported from the Worker's fetchGuestSession(). Returns None if the
    share isn't public or TeraBox's wap endpoint has changed shape (caller
    treats that as "this method doesn't work for this link" and moves on
    to the next fallback, same as the other extraction methods here)."""
    short_url = surl[1:] if (surl.startswith("1") and len(surl) > 20) else surl

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        fl_url = f"https://www.terabox.app/wap/share/filelist?surl={short_url}"
        async with session.get(fl_url, headers={"User-Agent": GUEST_UA}) as fl_resp:
            fl_text = (await fl_resp.text(errors="ignore")).replace("\\", "")
            guest_cookie = _extract_set_cookies(fl_resp)

        m = re.search(r'%28%22(.*?)%22%29', fl_text)
        if not m or not guest_cookie:
            return None
        js_token = m.group(1)

        info_url = f"https://www.terabox.com/api/shorturlinfo?app_id=250528&shorturl=1{short_url}&root=1"
        async with session.get(info_url, headers={"User-Agent": GUEST_UA, "Cookie": guest_cookie}) as info_resp:
            try:
                info = await info_resp.json(content_type=None)
            except Exception:
                return None

    if not info or info.get("errno") or not info.get("list"):
        return None
    return {"info": info, "js_token": js_token, "guest_cookie": guest_cookie}


async def _get_guest_download_link(fs_id, uk, shareid, timestamp, sign, js_token, guest_cookie) -> str | None:
    """Ported from the Worker's getGuestDownloadLink()."""
    params = {
        "uk": str(uk), "sign": str(sign or ""), "shareid": str(shareid), "primaryid": str(shareid),
        "timestamp": str(timestamp or ""), "jsToken": js_token, "fid_list": f"[{fs_id}]",
        "app_id": "250528", "channel": "dubox", "product": "share", "clienttype": "0",
        "dp-logid": "", "nozip": "0", "web": "1",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(
                "https://www.terabox.com/share/download", params=params,
                headers={"User-Agent": GUEST_UA, "Cookie": guest_cookie},
            ) as resp:
                data = await resp.json(content_type=None)
        if not data or data.get("errno"):
            return None
        return data.get("dlink") or None
    except Exception:
        return None


async def _fetch_via_guest_mode(url: str):
    """Cookie-free fallback for any PUBLIC TeraBox share — no NDUS login
    cookie needed at all, ours or a public pool's; see the module docstring
    above. Returns a list of (url, filename) tuples, same shape as the
    other two extraction methods, so it drops straight into
    _extract_terabox_files's fallback chain. Raises ValueError if the
    share isn't public, or files were found but no dlink could be
    resolved for any of them."""
    surl = _resolve_surl(url)
    if not surl:
        raise ValueError("couldn't read a share ID (surl) from this link")

    session_data = await _fetch_guest_session(surl)
    if not session_data:
        raise ValueError("guest session extraction failed (share may be private, or TeraBox changed its wap endpoint)")

    info = session_data["info"]
    js_token, guest_cookie = session_data["js_token"], session_data["guest_cookie"]
    raw_files = [f for f in (info.get("list") or []) if str(f.get("isdir")) != "1"]
    if not raw_files:
        raise ValueError("no files found via guest session")

    results = []
    for item in raw_files:
        dlink = await _get_guest_download_link(
            item.get("fs_id"), info.get("uk"), info.get("shareid"),
            info.get("timestamp"), info.get("sign"), js_token, guest_cookie,
        )
        if dlink:
            filename = safe_filename(item.get("server_filename"), "terabox_file")
            results.append((dlink, filename))

    if not results:
        raise ValueError("guest session found files but couldn't get any download link")
    return results



async def _filename_from_headers(url: str) -> str | None:
    """Best-effort filename lookup via a HEAD request — teradownloader's
    scraped CDN links don't come with a filename attached the way the
    savetube API response does, but the CDN itself usually reveals one
    through Content-Disposition (or, failing that, the URL path)."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.head(url, allow_redirects=True) as resp:
                cd = resp.headers.get("Content-Disposition", "")
                m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd)
                if m:
                    return safe_filename(unquote(m.group(1)), "terabox_file")
                base = os.path.basename(urlparse(str(resp.url)).path)
                if base:
                    return safe_filename(base, "terabox_file")
    except Exception:
        pass
    return None


# Known TeraBox CDN domains — used to tell the real file link apart from
# teradownloader.com's own nav/preview/ad links when the primary selector
# below doesn't match cleanly.
_CDN_DOMAIN_HINTS = (
    "1024tera", "freeterabox", "terabox.app", "terabox.com",
    "4funbox", "nephobox", "terabox.link",
)


async def _collect_candidate_hrefs(page):
    """Gathers every plausible download link on the rendered page, along with
    a best-effort display name for each (used for folder links, where the
    page renders one row per file — a single-file link just ends up as a
    list of one). Tries the specific selector the site currently uses
    first, then falls back to scanning every anchor if that selector
    doesn't match (site markup may have shifted slightly) — better to
    over-collect here and filter/verify below than to miss a real link
    because of one narrow selector.

    Returns a list of (href, display_name_or_None) tuples, in document
    order, with hrefs de-duplicated (first name seen wins)."""
    items = []
    seen = set()

    def _add(href, name):
        if href and href.startswith("http") and href not in seen:
            seen.add(href)
            items.append((href, name or None))

    try:
        # Folder pages render one block per file; grabbing the block's own
        # text alongside its link lets us recover a filename per row instead
        # of just one link for the whole page.
        rows = await page.query_selector_all("div.p-5")
        for row in rows:
            anchors = await row.query_selector_all("a")
            if not anchors:
                continue
            row_text = None
            try:
                row_text = (await row.inner_text() or "").strip() or None
            except Exception:
                pass
            for a in anchors:
                href = await a.get_attribute("href")
                # Prefer the anchor's own visible text as the name; fall
                # back to the row's text if the anchor itself has none.
                a_text = None
                try:
                    a_text = (await a.inner_text() or "").strip() or None
                except Exception:
                    pass
                _add(href, a_text or row_text)
    except Exception:
        pass

    if not items:
        try:
            anchors = await page.query_selector_all("a")
            for a in anchors:
                href = await a.get_attribute("href")
                if href and "teradownloader.com" not in href:
                    a_text = None
                    try:
                        a_text = (await a.inner_text() or "").strip() or None
                    except Exception:
                        pass
                    _add(href, a_text)
        except Exception:
            pass

    return items


def _rank_candidates(items):
    """Puts items whose href matches a known TeraBox CDN domain first (most
    likely to be real files), keeping the rest as a lower-priority
    fallback. Operates on (href, name) tuples and preserves order within
    each group."""
    preferred = [it for it in items if any(hint in it[0] for hint in _CDN_DOMAIN_HINTS)]
    rest = [it for it in items if it not in preferred]
    return preferred + rest


async def _head_ok(url: str) -> bool:
    """Confirms a candidate link actually resolves to a downloadable
    response before we commit to it and start streaming it to the user."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.head(url, allow_redirects=True) as resp:
                return resp.status in (200, 206)
    except Exception:
        return False


async def _render_and_collect(page_url: str, timeout: int):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            executable_path=system_chromium_path() if system_chromium_path else None,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            context = await browser.new_context(user_agent=_TERADOWNLOADER_UA)
            page = await context.new_page()
            await page.goto(page_url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                await page.wait_for_selector("div.p-5 a", timeout=timeout * 1000)
            except Exception:
                pass  # fall through to the broader any-anchor scan below regardless
            # The link is sometimes populated a beat after the selector
            # first appears (async JS finishing up) — give it a moment.
            await page.wait_for_timeout(1500)
            return await _collect_candidate_hrefs(page)
        finally:
            await browser.close()


def _name_to_filename(name: str) -> str | None:
    """Turns a row's visible text (which may include size/date noise
    alongside the actual filename) into a clean filename, if the text
    looks like it contains one."""
    if not name:
        return None
    # The filename is usually the first "word group" on the row; take the
    # longest whitespace-delimited chunk that contains a dot-extension,
    # since surrounding text (file size, date, "Download" button label) is
    # what's most likely to lack one.
    candidates = re.findall(r"[^\s]+\.[A-Za-z0-9]{2,5}", name)
    if candidates:
        return safe_filename(max(candidates, key=len), None)
    return None


async def _fetch_all_via_teradownloader(link: str, timeout: int = 25):
    """Extracts direct TeraBox CDN download link(s) via teradownloader.com.

    teradownloader.com resolves the actual TeraBox CDN link(s) entirely in
    client-side JS, so this renders the page in headless Chromium via
    Playwright rather than scraping static HTML (which only ever shows a
    "Loading..." placeholder). For a single-file share link this yields one
    file; for a folder share link the same rendered page lists one row per
    file, so every verified candidate is returned rather than just the
    first — this is what gives us folder support.

    Every candidate link found is verified with a HEAD request before being
    accepted, and the whole render is retried once if the first attempt
    turns up nothing valid (the site is occasionally slow to populate
    links). Raises ValueError if nothing usable is found after that, so the
    caller can report the failure.

    Returns a list of (url, filename) tuples — one entry for a single file,
    multiple for a folder.
    """
    if async_playwright is None:
        raise ValueError("Playwright not installed — teradownloader unavailable.")

    if _ensure_chromium is not None:
        try:
            # Hard cap: on hosts where the browser binary/deps are missing
            # or broken (e.g. Replit's default Nix env, which the
            # Dockerfile's build-time `playwright install --with-deps
            # chromium` never runs on), this call can otherwise hang far
            # longer than any user will wait, with the status message
            # stuck on "extracting..." forever and no error ever shown.
            await asyncio.wait_for(_ensure_chromium(), timeout=60)
        except asyncio.TimeoutError:
            raise ValueError(
                "Chromium setup timed out on this host. If you're on Replit, "
                "the Dockerfile's 'playwright install --with-deps chromium' step "
                "never runs there — the browser's system libraries are likely missing."
            )

    page_url = f"https://teradownloader.com/download?l={quote(link, safe='')}"

    last_error = None
    for attempt in range(2):  # one retry if the page was just slow
        try:
            # Same reasoning as above: cap each render attempt so a broken/
            # missing Chromium install fails fast with a real error instead
            # of hanging indefinitely on browser.launch().
            items = await asyncio.wait_for(_render_and_collect(page_url, timeout), timeout=timeout + 20)
        except asyncio.TimeoutError:
            last_error = "browser render timed out (chromium may be missing its system libraries on this host)"
            continue
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "missing dependencies" in msg.lower():
                raise ValueError(
                    "Chromium isn't properly installed on this host (browser binary or its "
                    "system libraries are missing). This commonly happens on Replit's default "
                    "environment, which skips the Dockerfile's chromium setup step."
                )
            last_error = msg
            continue

        results = []
        for href, name in _rank_candidates(items):
            if await _head_ok(href):
                filename = _name_to_filename(name) or await _filename_from_headers(href) or safe_filename(None, "terabox_file")
                results.append((href, filename))

        if results:
            return results

        last_error = "page rendered but no candidate link responded to a HEAD request"

    raise ValueError(
        f"teradownloader: no working download link found after 2 attempts "
        f"(site markup may have changed, or the link is invalid/private). Last issue: {last_error}"
    )


async def _extract_terabox_files(url: str):
    """Tries progressively heavier/more-dependent methods, stopping at the
    first one that returns something usable:
      1. Direct scrape of the share page's own HTML (fastest, no browser
         or session needed at all).
      2. Guest-session mode — still no browser and no login cookie needed,
         just TeraBox's own public wap/api endpoints (see
         _fetch_via_guest_mode's docstring). No HLS this way, but that's
         irrelevant here since we only ever need a direct dlink.
      3. teradownloader.com rendered via headless Chromium (heaviest, and
         known-fragile on hosts without a working browser install, e.g.
         Replit's default environment) — last resort.
    """
    errors = []
    try:
        return await _fetch_via_direct_scrape(url)
    except Exception as e:
        errors.append(f"direct scrape failed ({e})")

    try:
        return await _fetch_via_guest_mode(url)
    except Exception as e:
        errors.append(f"guest mode failed ({e})")

    try:
        return await _fetch_all_via_teradownloader(url)
    except Exception as e:
        errors.append(f"teradownloader.com fallback also failed ({e})")

    raise ValueError("; ".join(errors))


async def _handle(client: Client, message: Message, url: str):
    status = await message.reply_text(f"<b>{E_INFO} TeraBox link detected — extracting...</b>", parse_mode=enums.ParseMode.HTML)
    if await try_send_cached(client, message, url, status):
        return
    try:
        files = await _extract_terabox_files(url)
    except Exception as e:
        return await status.edit_text(f"<b>{E_CROSS} Error:</b>\n<code>{e}</code>", parse_mode=enums.ParseMode.HTML)

    folder = make_output_folder("terabox")
    total = len(files)
    is_folder = total > 1
    ok_count = 0

    for idx, (direct_url, filename) in enumerate(files, start=1):
        prefix = f"[{idx}/{total}] " if is_folder else ""
        try:
            # message.id is only unique WITHIN a single chat, not globally,
            # so two users whose messages happen to share an id would
            # otherwise collide on the same filename in this shared
            # folder; include chat.id, and the file index for folders, to
            # keep every destination path globally unique.
            dest = f"{folder}/{message.chat.id}_{message.id}_{idx}_{filename}"
            await stream_download(
                direct_url, dest, status, f"{prefix}Downloading from TeraBox",
                user_id=message.from_user.id, file_name=filename
            )
            caption = f"<b>{E_CHECK} TeraBox File</b>\n<code>{filename}</code>"
            if is_folder:
                caption += f"\n<i>{idx}/{total}</i>"
            await upload_file(client, message, dest, status, caption, file_name=filename, cache_url=(url if not is_folder else None))
            ok_count += 1
        except Exception as e:
            # One bad file in a folder shouldn't stop the rest from being
            # fetched — report it and continue to the next.
            await message.reply_text(
                f"<b>{E_CROSS} Failed:</b> <code>{filename}</code>\n<code>{e}</code>",
                parse_mode=enums.ParseMode.HTML
            )

    if is_folder:
        await status.edit_text(
            f"<b>{E_CHECK} TeraBox folder done:</b> {ok_count}/{total} file(s) delivered.",
            parse_mode=enums.ParseMode.HTML
        )


@Client.on_message(filters.text & filters.private & filters.regex(PATTERN), group=1)
async def terabox_auto_detect(client: Client, message: Message):
    url = extract_url(message.text)
    if url:
        await _handle(client, message, url)


@Client.on_message(filters.command("terabox") & filters.private)
async def terabox_command(client: Client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> <code>/terabox &lt;terabox URL&gt;</code>",
            parse_mode=enums.ParseMode.HTML
        )
    url = extract_url(message.command[1]) or message.command[1]
    await _handle(client, message, url)
