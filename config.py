"""
Save Restricted Content Bot Configuration

Developed by: Anuj Kumar
Telegram: @Anujedits76

Please retain this credit if you use or modify this project.
"""

import os


def _require(name: str, default: str = "") -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable/secret: {name}.")
    return value


# ==============================
# Telegram Bot Credentials
# ==============================

BOT_TOKEN = _require("BOT_TOKEN", "8638965974:AAGY3oQ4e8rNqzmNvHJsqJglpu_3xfRZNSE")
API_ID = int(_require("API_ID", "37476811"))
API_HASH = _require("API_HASH", "7aa60670b871050820086c6267371ee6")


# ==============================
# Admin Configuration
# ==============================

# Add admin user IDs separated by commas in environment variables.
# No hardcoded fallback here on purpose: /shell and /eval (devtools.py) are
# gated on ADMINS, so silently defaulting to a baked-in ID would give that
# ID admin access (including those dangerous commands) on any deployment
# that forgets to set ADMINS explicitly. Failing loudly is safer.
ADMINS = [int(admin) for admin in _require("ADMINS", "8730393744").split(",") if admin]


# ==============================
# Database Configuration
# ==============================

DB_URI = _require("DB_URI", "mongodb+srv://Anujedit:Anujedit@cluster0.7cs2nhd.mongodb.net/?appName=Cluster0")
DB_NAME = os.environ.get("DB_NAME", "SaveRestricted2")


# ==============================
# Logging Configuration
# ==============================

# Telegram channel ID the bot logs to (example: -1001234567890)
LOG_CHANNEL = int(_require("LOG_CHANNEL", "-1003824246703"))

# --- JDownloader (/jd) — covers hundreds of hosts yt-dlp doesn't. ---
# Free account at https://my.jdownloader.org — see JDOWNLOADER_SETUP.md.
# Leave both blank to disable /jd entirely (nothing else is affected).
JD_EMAIL = os.environ.get("JD_EMAIL", "editsbyanuj@gmail.com")
JD_PASS = os.environ.get("JD_PASS", "fjagykga")
JD_DOWNLOAD_DIR = os.environ.get("JD_DOWNLOAD_DIR", "/JDownloader/downloads")

# ==============================
# Error Handling
# ==============================

# Set to True to send error messages to users
ERROR_MESSAGE = os.environ.get("ERROR_MESSAGE", "True").lower() == "true"

# ==============================
# Batch Link Limits
# ==============================

# Hard safety caps on how many messages a single batch link can request
MAX_BATCH_IDS_FREE    = int(os.environ.get("MAX_BATCH_IDS_FREE", "50"))
MAX_BATCH_IDS_PREMIUM = int(os.environ.get("MAX_BATCH_IDS_PREMIUM", "200"))

# Selectable options shown in the Settings > Batch Limit menu
BATCH_LIMIT_OPTIONS_FREE    = [10, 25, 50]
BATCH_LIMIT_OPTIONS_PREMIUM = [50, 100, 150, 200]

# ==============================
# YouTube / Instagram Downloader
# ==============================

# Max direct-download file size the bot will accept (bytes). This used to be
# capped at 2GB (Telegram's bot-upload limit), but Akbots/direct_utils.py
# now auto-splits anything over SPLIT_SIZE (1.9GB) into parts before
# uploading, so this can safely go higher — it's just guarding against
# absurdly large / abusive downloads, not the per-file Telegram limit anymore.
YTDL_MAX_FILESIZE = int(os.environ.get("YTDL_MAX_FILESIZE", str(4 * 1024 * 1024 * 1024)))  # 4GB

# Optional — routes yt-dlp's traffic through a proxy (e.g.
# "http://user:pass@host:port" or "socks5://host:port"). Only needed if a
# CDN/site blocks this host's IP or region outright; leave unset otherwise.
YTDLP_PROXY = os.environ.get("YTDLP_PROXY", "").strip()

# Optional GoFile account token (Akbots/gofile.py). Without it, /gofile
# still works for public links via a temporary guest session — this just
# authenticates as your account instead (higher rate limits, and access to
# content tied to your account). Get it from https://gofile.io/myProfile.
GOFILE_TOKEN = os.environ.get("GOFILE_TOKEN", "VmueEQVJ07tbmbVLYHAzxbVrkcdY0gLD")

# Optional Spotify Web API app credentials (Akbots/spotify.py's text
# search, e.g. "/spotify believer"). Free, official, no user login needed —
# register a throwaway app at https://developer.spotify.com/dashboard,
# grab its Client ID + Client Secret, done. Without these, /spotify still
# works fine for pasted open.spotify.com links — it's only the "search by
# song name" feature that needs them (Spotify doesn't expose search on the
# unofficial spotidown.app resolver this bot otherwise uses).
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "c578a019aad14d2ab8903a33466b79df")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "28a212185ce2497bb2231d3dccfb289f")
YT_COOKIES    = os.environ.get("YT_COOKIES", "youtube/yt_cookies.txt")       # Netscape-format cookies.txt

# Google Drive OAuth token (enables /gdrive folder + private-file support).
# Generated locally by gdrive_oauth_setup.py, then uploaded to the bot via
# /setgdrivetoken. If missing, /gdrive just uses the public-file-only
# fallback that already worked before — nothing breaks.
GDRIVE_TOKEN_PATH = os.environ.get("GDRIVE_TOKEN_PATH", "gdrive/token.pickle")
INSTA_COOKIES = os.environ.get("INSTA_COOKIES", "instagram/insta_cookies.txt")
FB_COOKIES    = os.environ.get("FB_COOKIES", "facebook/fb_cookies.txt")
# VK.com — only needed for private/age-restricted videos; public videos and
# clips work with no cookies at all. See Akbots/vk.py.
VK_COOKIES    = os.environ.get("VK_COOKIES", "vk/vk_cookies.txt")

# ==============================
# YouTube Search (/search)
# ==============================

YTDL_SEARCH_PAGE_SIZE = int(os.environ.get("YTDL_SEARCH_PAGE_SIZE", "10"))

# ==============================
# Free-Access Token Gate (optional, URL-shortener based)
# ==============================

# Leave WEBSITE_URL / AD_API empty to keep this feature fully disabled.
WEBSITE_URL = os.environ.get("WEBSITE_URL", "")
AD_API      = os.environ.get("AD_API", "")
TOKEN_VALID_HOURS = int(os.environ.get("TOKEN_VALID_HOURS", "3"))
TOKEN_BATCH_BONUS = int(os.environ.get("TOKEN_BATCH_BONUS", "20"))

# ==============================
# Developer Tools (owner-only /eval, /shell)
# ==============================

# Extremely powerful — only ADMINS can ever use these regardless of this flag.
DEV_TOOLS_ENABLED = os.environ.get("DEV_TOOLS_ENABLED", "True").lower() == "true"

# ==============================
# Telegram Stars Payment Plans (/pay)
# ==============================

# label, days, star price — edit freely
STAR_PLANS = {
    "d": {"label": "1 Day",   "days": 1,  "stars": int(os.environ.get("STAR_PRICE_DAY", "15"))},
    "w": {"label": "1 Week",  "days": 7,  "stars": int(os.environ.get("STAR_PRICE_WEEK", "75"))},
    "m": {"label": "1 Month", "days": 30, "stars": int(os.environ.get("STAR_PRICE_MONTH", "250"))},
}

# ==============================
# Bot Mode (Freemium / Paid)
# ==============================

DEFAULT_BOT_MODE = os.environ.get("DEFAULT_BOT_MODE", "paid")  # "paid" or "freemium"

# ==============================
# Referral Program
# ==============================

REFERRAL_REWARD_BUCKS = int(os.environ.get("REFERRAL_REWARD_BUCKS", "50"))   # earned per successful referral
REFERRAL_TRIAL_DAYS   = int(os.environ.get("REFERRAL_TRIAL_DAYS", "1"))      # trial premium given to the new joiner
BUCKS_PER_PREMIUM_DAY = int(os.environ.get("BUCKS_PER_PREMIUM_DAY", "100"))  # redemption rate

# ==============================
# Force Subscribe (optional)
# ==============================
# Set to a channel ID/username (the bot must be an admin there) to require
# users to join before using the bot. Leave empty to keep this disabled.
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "")

# ==============================
# Movie Info / Poster / AutoPost (optional, TMDB-powered)
# ==============================
# Get a free API key at https://www.themoviedb.org/settings/api
# Leave TMDB_API_KEY empty to keep /movieinfo, /poster and autopost disabled.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# Channel where the daily autopost job publishes movie release updates.
# Leave empty to keep autopost disabled even if TMDB_API_KEY is set.
AUTOPOST_CHANNEL = os.environ.get("AUTOPOST_CHANNEL", "")

# Hour (UTC, 0-23) the daily autopost job runs at.
AUTOPOST_HOUR_UTC = int(os.environ.get("AUTOPOST_HOUR_UTC", "6"))

# ==============================
# Auto-Backup (Akbots/backup.py)
# ==============================

# Channel every completed download is auto-copied to, and where the daily
# database dump is posted. Falls back to LOG_CHANNEL so this works out of
# the box, but you can point it at a dedicated private channel instead by
# setting DB_CHANNEL in the environment.
DB_CHANNEL = int(os.environ.get("DB_CHANNEL", "") or LOG_CHANNEL)

# Set to False to stop copying every finished file to DB_CHANNEL (the daily
# DB dump below is unaffected by this flag).
AUTO_BACKUP_FILES = os.environ.get("AUTO_BACKUP_FILES", "True").lower() == "true"

# Hour (UTC, 0-23) the daily users-collection backup runs at.
DB_BACKUP_HOUR_UTC = int(os.environ.get("DB_BACKUP_HOUR_UTC", "3"))


# ==============================
# File Store (Akbots/filestore.py, Akbots/auto_batch.py)
# ==============================
# Master switch — set False to disable all file-store commands/handlers.
FILESTORE_ENABLED = os.environ.get("FILESTORE_ENABLED", "True").lower() == "true"

# Extra DB channels for the multi-DB round-robin system, on top of
# DB_CHANNEL above (which is always slot #1 in the rotation). Comma
# separated chat IDs, e.g. "-1001111111111,-1002222222222". Round-robin
# only kicks in once /multidb is turned on (off by default).
FILESTORE_EXTRA_DB_CHANNELS = [
    int(c) for c in os.environ.get("FILESTORE_EXTRA_DB_CHANNELS", "").split(",") if c.strip()
]

# Auto-batch: default time window (seconds) within which quality variants
# of the same file uploaded to a DB channel get grouped into one batch
# link automatically. Overridable at runtime via /autobatch window <secs>.
FILESTORE_AUTO_BATCH_WINDOW = int(os.environ.get("FILESTORE_AUTO_BATCH_WINDOW", "30"))

# Minutes a share link stays open to a user after they solve the
# shortener, before they'd need to click through again.
FILESTORE_ACCESS_TOKEN_MINUTES = int(os.environ.get("FILESTORE_ACCESS_TOKEN_MINUTES", "10"))

# Seconds after delivery before a shared file auto-deletes from the
# user's chat (0 disables auto-delete).
FILESTORE_AUTO_DELETE_SECONDS = int(os.environ.get("FILESTORE_AUTO_DELETE_SECONDS", "0"))

# Auto-generate missing qualities: when a batch link would only cover 1
# quality (no siblings found in the DB channel), re-encode the source
# with ffmpeg into the qualities below that are smaller than the source
# and don't already exist, upload each, and batch them all together.
# Off by default — this costs real CPU/time per link, so it's opt-in via
# /autogenerate on.
FILESTORE_AUTO_GENERATE_QUALITIES = [
    q.strip() for q in os.environ.get(
        "FILESTORE_AUTO_GENERATE_QUALITIES", "144p,240p,360p,480p,720p,1080p,4K"
    ).split(",") if q.strip()
]
# URL shortener gate — leave FILESTORE_SHORTENER_API_TOKEN empty to keep
# links opening directly with no shortener step (gate stays off even if
# /shortener on is run). Any text-response shortener API works here
# (VPLink, GPLinks, ShrinkMe, etc.) — same response contract as the
# shorten_url() plugins the original File-Store project shipped with.
FILESTORE_SHORTENER_NAME = os.environ.get("FILESTORE_SHORTENER_NAME", "VPLink")
FILESTORE_SHORTENER_API_URL = os.environ.get("FILESTORE_SHORTENER_API_URL", "https://vplink.in/api")
FILESTORE_SHORTENER_API_TOKEN = os.environ.get("FILESTORE_SHORTENER_API_TOKEN", "1064cf16abef338e46ba40f2501c130dd2b94d19")
