# Akbots - Don't Remove Credit - @AkBots_Official
#
# Archive tools — /unzip and /zip.
#
# /unzip  — reply to any archive (zip/rar/7z/tar/gz/bz2/xz/...) and the bot
#           extracts it with the `7z` CLI (same approach as the standalone
#           Unzipper-Bot project) and uploads every extracted file back,
#           reusing direct_utils.upload_file for the actual send (so it
#           gets the same progress bar / >1.9GB auto-split as every other
#           downloader here). Password-protected archives: /unzip <password>
#           as a reply.
#
# /zip    — batch archive creation (ported from Zip-Maker-Bot's zipmaker
#           module). /zip starts a collection session; every document/
#           video/audio/photo the user sends afterwards gets downloaded and
#           queued. /zipname sets a custom archive name, /zippass sets an
#           AES-256 password (via pyzipper — falls back to a plain zip if
#           pyzipper isn't installed), /donezip builds + uploads the
#           archive, /zipcancel aborts and wipes the temp files.
#
# Needs `7z` on PATH for /unzip (p7zip-full, + p7zip-rar or unrar for RAR
# support — see Dockerfile). /zip has no system dependency: it's pure
# Python (zipfile, optionally pyzipper).

import os
import time
import shutil
import asyncio
import zipfile
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from database.db import db

from Akbots.direct_utils import (
    make_output_folder, safe_filename, upload_file, fmt_bytes,
    E_CHECK, E_CROSS, E_INFO, E_BOLT, E_ROCKET
)
from Akbots import task_manager

try:
    import pyzipper
    PYZIPPER_AVAILABLE = True
except ImportError:
    PYZIPPER_AVAILABLE = False

E_PACK   = '📦'
E_WARN   = '<emoji id=5447644880824181073>⚠️</emoji>'
E_TRASH  = '<emoji id=5260293700088511294>🗑</emoji>'
E_FILE   = '📄'

# user_id -> {"paths": [...], "name": str|None, "password": str|None, "started": float}
_ZIP_SESSIONS = {}


def _media_of(message: Message):
    return message.document or message.video or message.audio or message.photo or message.voice


def _media_name(message: Message, fallback: str) -> str:
    media = _media_of(message)
    name = getattr(media, "file_name", None)
    if name:
        return safe_filename(name, fallback)
    ext = ".jpg" if message.photo else ".ogg" if message.voice else ".bin"
    return fallback + ext


# ============================================================
# /unzip — extraction
# ============================================================

async def _run_7z_extract(archive_path: str, out_dir: str, password: str = ""):
    os.makedirs(out_dir, exist_ok=True)
    cmd = ["7z", "x", f"-o{out_dir}", "-y"]
    cmd.append(f"-p{password}" if password else "-p-")  # -p- => no prompt, empty pw if none given
    cmd.append(archive_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    # 7z exit codes: 0=OK, 1=Warning (still usable), 2+=fatal
    return proc.returncode, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")


@Client.on_message(filters.private & filters.command("unzip"))
async def unzip_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    reply = message.reply_to_message
    if not reply or not _media_of(reply):
        return await message.reply_text(
            f"<b>{E_INFO} Usage:</b> reply to an archive file (zip/rar/7z/tar/gz/...) with "
            f"<code>/unzip</code> — add a password after it if the archive needs one, e.g. "
            f"<code>/unzip mypassword</code>.",
            parse_mode=enums.ParseMode.HTML
        )

    password = message.text.split(" ", 1)[1].strip() if len(message.command) > 1 else ""

    if shutil.which("7z") is None:
        return await message.reply_text(
            f"<b>{E_CROSS} The <code>7z</code> tool isn't installed on this server.</b> "
            f"Install <code>p7zip-full</code> (and <code>p7zip-rar</code>/<code>unrar</code> for RAR) "
            f"and redeploy.",
            parse_mode=enums.ParseMode.HTML
        )

    status = await message.reply_text(f"<b>{E_INFO} Downloading archive...</b>", parse_mode=enums.ParseMode.HTML)

    session_dir = os.path.join(make_output_folder("unzip"), f"{user_id}_{message.id}")
    os.makedirs(session_dir, exist_ok=True)
    arc_name = _media_name(reply, "archive")
    arc_path = os.path.join(session_dir, arc_name)
    out_dir = os.path.join(session_dir, "extracted")

    async def _job():
        try:
            await reply.download(file_name=arc_path)
            await status.edit_text(f"<b>{E_BOLT} Extracting...</b>", parse_mode=enums.ParseMode.HTML)

            code, out, err = await _run_7z_extract(arc_path, out_dir, password)
            if code not in (0, 1):
                hint = ""
                if "Wrong password" in err or "Wrong password" in out:
                    hint = " (looks like a wrong/missing password — retry with <code>/unzip &lt;password&gt;</code> as a reply)"
                return await status.edit_text(
                    f"<b>{E_CROSS} Extraction failed.</b>{hint}\n<code>{(err or out)[-500:]}</code>",
                    parse_mode=enums.ParseMode.HTML
                )

            files = []
            for root, _, names in os.walk(out_dir):
                for n in names:
                    files.append(os.path.join(root, n))

            if not files:
                return await status.edit_text(
                    f"<b>{E_WARN} Archive extracted but no files were found inside.</b>",
                    parse_mode=enums.ParseMode.HTML
                )

            await status.edit_text(
                f"<b>{E_ROCKET} Extracted {len(files)} file(s) — uploading...</b>",
                parse_mode=enums.ParseMode.HTML
            )
            for i, fpath in enumerate(files, start=1):
                rel = os.path.relpath(fpath, out_dir)
                cap = f"<b>{E_FILE} {rel}</b>\n<i>({i}/{len(files)} from {arc_name})</i>"
                up_status = await message.reply_text(f"<b>{E_ROCKET} Uploading {rel}...</b>", parse_mode=enums.ParseMode.HTML)
                try:
                    await upload_file(client, message, fpath, up_status, cap, file_name=os.path.basename(fpath))
                except Exception as e:
                    await up_status.edit_text(f"<b>{E_CROSS} Failed to upload {rel}:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)

            await status.edit_text(f"<b>{E_CHECK} Done — all files sent.</b>", parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            await status.edit_text(f"<b>{E_CROSS} Error:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)
        finally:
            shutil.rmtree(session_dir, ignore_errors=True)

    task = asyncio.ensure_future(_job())
    task_id = task_manager.register(user_id, task, f"Unzip: {arc_name}")
    task.add_done_callback(lambda t: task_manager.unregister(user_id, task_id))


# ============================================================
# /zip — batch creation
# ============================================================

@Client.on_message(filters.private & filters.command("zip"))
async def zip_start_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    if not await db.is_user_exist(user_id):
        await db.add_user(user_id, message.from_user.first_name)

    if user_id in _ZIP_SESSIONS:
        return await message.reply_text(
            f"<b>{E_WARN} You already have a zip session running</b> "
            f"({len(_ZIP_SESSIONS[user_id]['paths'])} file(s) queued).\n"
            f"Send more files, or use <code>/donezip</code> / <code>/zipcancel</code>.",
            parse_mode=enums.ParseMode.HTML
        )

    _ZIP_SESSIONS[user_id] = {"paths": [], "name": None, "password": None, "started": time.time()}
    await message.reply_text(
        f"<b>{E_PACK} Zip session started.</b>\n\n"
        f"Send me the files you want archived (document/video/audio/photo), then:\n"
        f"➢ <code>/zipname mybackup</code> — set archive name (optional)\n"
        f"➢ <code>/zippass secret</code> — password-protect with AES-256 (optional)\n"
        f"➢ <code>/donezip</code> — build and send the archive\n"
        f"➢ <code>/zipcancel</code> — abort",
        parse_mode=enums.ParseMode.HTML
    )


@Client.on_message(filters.private & filters.command("zipname"))
async def zip_name_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    session = _ZIP_SESSIONS.get(user_id)
    if not session:
        return await message.reply_text(f"<b>{E_WARN} No active zip session.</b> Start one with <code>/zip</code>.", parse_mode=enums.ParseMode.HTML)
    if len(message.command) < 2:
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/zipname mybackup</code>", parse_mode=enums.ParseMode.HTML)
    session["name"] = safe_filename(message.text.split(" ", 1)[1].strip(), "archive")
    await message.reply_text(f"<b>{E_CHECK} Archive name set:</b> <code>{session['name']}.zip</code>", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command("zippass"))
async def zip_pass_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    session = _ZIP_SESSIONS.get(user_id)
    if not session:
        return await message.reply_text(f"<b>{E_WARN} No active zip session.</b> Start one with <code>/zip</code>.", parse_mode=enums.ParseMode.HTML)
    if len(message.command) < 2:
        return await message.reply_text(f"<b>{E_INFO} Usage:</b> <code>/zippass secret</code>", parse_mode=enums.ParseMode.HTML)
    if not PYZIPPER_AVAILABLE:
        return await message.reply_text(
            f"<b>{E_WARN} <code>pyzipper</code> isn't installed</b> — password-protected zips aren't "
            f"available. Add <code>pyzipper</code> to requirements.txt and redeploy.",
            parse_mode=enums.ParseMode.HTML
        )
    session["password"] = message.text.split(" ", 1)[1].strip()
    await message.reply_text(f"<b>{E_CHECK} Password set.</b> Archive will use AES-256 encryption.", parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.command("zipcancel"))
async def zip_cancel_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    session = _ZIP_SESSIONS.pop(user_id, None)
    if not session:
        return await message.reply_text(f"<b>{E_WARN} No active zip session.</b>", parse_mode=enums.ParseMode.HTML)
    for p in session["paths"]:
        try:
            os.remove(p)
        except OSError:
            pass
    await message.reply_text(f"<b>{E_TRASH} Zip session cancelled — {len(session['paths'])} file(s) discarded.</b>", parse_mode=enums.ParseMode.HTML)


# group=-1 so this runs before rename.py's catch-all document handler, but
# only ever fires (and only ever stops propagation) when a /zip session is
# actually active for this user — any other file upload passes straight
# through untouched, exactly like cookies_manager.py's pending-flow pattern.
@Client.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo | filters.voice), group=-1)
async def zip_collect_file(client: Client, message: Message):
    user_id = message.from_user.id
    session = _ZIP_SESSIONS.get(user_id)
    if not session:
        return

    folder = make_output_folder(f"zip_session/{user_id}")
    idx = len(session["paths"]) + 1
    fname = _media_name(message, f"file_{idx}")
    dest = os.path.join(folder, f"{idx}_{fname}")

    status = await message.reply_text(f"<b>{E_BOLT} Adding {fname} to archive...</b>", parse_mode=enums.ParseMode.HTML)
    try:
        await message.download(file_name=dest)
        session["paths"].append(dest)
        await status.edit_text(
            f"<b>{E_CHECK} Added:</b> <code>{fname}</code>\n"
            f"<b>Queued:</b> {len(session['paths'])} file(s)\n\n"
            f"<i>Send more, or use /donezip when ready.</i>",
            parse_mode=enums.ParseMode.HTML
        )
    except Exception as e:
        await status.edit_text(f"<b>{E_CROSS} Failed to add file:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)
    message.stop_propagation()


@Client.on_message(filters.private & filters.command("donezip"))
async def zip_done_cmd(client: Client, message: Message):
    user_id = message.from_user.id
    session = _ZIP_SESSIONS.pop(user_id, None)
    if not session:
        return await message.reply_text(f"<b>{E_WARN} No active zip session.</b> Start one with <code>/zip</code>.", parse_mode=enums.ParseMode.HTML)
    if not session["paths"]:
        return await message.reply_text(f"<b>{E_WARN} No files were added.</b> Session cancelled.", parse_mode=enums.ParseMode.HTML)

    status = await message.reply_text(f"<b>{E_BOLT} Building archive...</b>", parse_mode=enums.ParseMode.HTML)
    zip_name = (session["name"] or f"AkbotsArchive_{int(time.time())}") + ".zip"
    zip_dir = make_output_folder(f"zip_out/{user_id}")
    zip_path = os.path.join(zip_dir, zip_name)

    async def _job():
        try:
            def _build():
                if session["password"] and PYZIPPER_AVAILABLE:
                    with pyzipper.AESZipFile(
                        zip_path, "w",
                        compression=pyzipper.ZIP_DEFLATED,
                        encryption=pyzipper.WZ_AES
                    ) as zf:
                        zf.setpassword(session["password"].encode("utf-8"))
                        zf.setencryption(pyzipper.WZ_AES, nbits=256)
                        for p in session["paths"]:
                            if os.path.exists(p):
                                zf.write(p, os.path.basename(p).split("_", 1)[-1])
                else:
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for p in session["paths"]:
                            if os.path.exists(p):
                                zf.write(p, os.path.basename(p).split("_", 1)[-1])

            await asyncio.to_thread(_build)

            size = os.path.getsize(zip_path)
            note = " (AES-256 encrypted)" if session["password"] and PYZIPPER_AVAILABLE else ""
            await status.edit_text(f"<b>{E_ROCKET} Uploading archive ({fmt_bytes(size)})...</b>", parse_mode=enums.ParseMode.HTML)
            await upload_file(
                client, message, zip_path, status,
                f"<b>{E_PACK} {zip_name}</b>{note}\n<b>Files:</b> {len(session['paths'])} | <b>Size:</b> {fmt_bytes(size)}",
                file_name=zip_name
            )
        except Exception as e:
            await status.edit_text(f"<b>{E_CROSS} Failed to build archive:</b> <code>{e}</code>", parse_mode=enums.ParseMode.HTML)
        finally:
            for p in session["paths"]:
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                if os.path.exists(zip_path):
                    os.remove(zip_path)
            except OSError:
                pass

    task = asyncio.ensure_future(_job())
    task_id = task_manager.register(user_id, task, f"Zip: {zip_name}")
    task.add_done_callback(lambda t: task_manager.unregister(user_id, task_id))
