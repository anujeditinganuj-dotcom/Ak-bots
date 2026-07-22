import io
import os
import sys
import glob
import shutil
import traceback
import subprocess
from pyrogram import Client, filters, enums
from pyrogram.types import Message
from config import ADMINS, DEV_TOOLS_ENABLED

E_CHECK = '<emoji id=5206607081334906820>✔️</emoji>'
E_CROSS = '<emoji id=5210952531676504517>❌</emoji>'
E_GEAR  = '<emoji id=5341715473882955310>⚙️</emoji>'


def _enabled():
    return DEV_TOOLS_ENABLED


async def _aexec(code: str, client: Client, message: Message):
    exec_globals = {"client": client, "message": message, "app": client}
    body = "\n".join(f"    {line}" for line in code.split("\n"))
    exec(f"async def __ex(client, message):\n{body}", exec_globals)
    return await exec_globals["__ex"](client, message)


# =========================================================
# /eval - Owner-only Python code execution (debug console)
# =========================================================

@Client.on_message(filters.command(["eval"]) & filters.user(ADMINS))
async def eval_command(client: Client, message: Message):
    if not _enabled():
        return await message.reply_text(f"<b>{E_CROSS} Dev tools are disabled.</b>", parse_mode=enums.ParseMode.HTML)
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_GEAR} Usage:</b> <code>/eval &lt;python code&gt;</code>", parse_mode=enums.ParseMode.HTML
        )

    code = message.text.split(None, 1)[1]
    old_stdout = sys.stdout
    sys.stdout = redirected = io.StringIO()
    try:
        result = await _aexec(code, client, message)
        output = redirected.getvalue()
        if result is not None:
            output += repr(result)
        if not output.strip():
            output = "(no output)"
    except Exception:
        output = traceback.format_exc()
    finally:
        sys.stdout = old_stdout

    if len(output) > 3500:
        output = output[:3500] + "\n... (truncated)"
    await message.reply_text(f"<pre>{output}</pre>", parse_mode=enums.ParseMode.HTML)


# =========================================================
# /shell - Owner-only host shell command execution
# =========================================================

@Client.on_message(filters.command(["shell", "sh"]) & filters.user(ADMINS))
async def shell_command(client: Client, message: Message):
    if not _enabled():
        return await message.reply_text(f"<b>{E_CROSS} Dev tools are disabled.</b>", parse_mode=enums.ParseMode.HTML)
    if len(message.command) < 2:
        return await message.reply_text(
            f"<b>{E_GEAR} Usage:</b> <code>/shell &lt;command&gt;</code>", parse_mode=enums.ParseMode.HTML
        )

    cmd = message.text.split(None, 1)[1]
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        output = (proc.stdout or "") + (proc.stderr or "")
        if not output.strip():
            output = f"(exit code {proc.returncode}, no output)"
    except subprocess.TimeoutExpired:
        output = "Command timed out after 60s."
    except Exception as e:
        output = f"Error: {e}"

    if len(output) > 3500:
        output = output[:3500] + "\n... (truncated)"
    await message.reply_text(f"<pre>{output}</pre>", parse_mode=enums.ParseMode.HTML)


# =========================================================
# /checkdeps - reports which optional runtime tools are actually available
# on this host (ffmpeg, aria2c, gallery-dl, yt-dlp, Playwright+Chromium),
# instead of everyone having to guess/dig through /shell output by hand.
# Not gated behind DEV_TOOLS_ENABLED (unlike /eval and /shell) since it's
# read-only and doesn't execute arbitrary code — just runs a handful of
# fixed, safe version/existence checks.
# =========================================================

def _bin_version(name: str, args=("--version",)) -> str:
    path = shutil.which(name)
    if not path:
        return None
    try:
        out = subprocess.run([name, *args], capture_output=True, text=True, timeout=10).stdout
        return (out.strip().splitlines() or [""])[0][:80]
    except Exception:
        return "(found, version check failed)"


@Client.on_message(filters.command(["checkdeps", "deps"]) & filters.user(ADMINS))
async def checkdeps_command(client: Client, message: Message):
    status = await message.reply_text(f"<b>{E_GEAR} Checking dependencies...</b>", parse_mode=enums.ParseMode.HTML)

    lines = ["<b>Runtime dependency check</b>\n"]

    for name, args in (("ffmpeg", ("-version",)), ("ffprobe", ("-version",)),
                       ("aria2c", ("--version",)), ("gallery-dl", ("--version",))):
        v = _bin_version(name, args)
        mark = E_CHECK if v else E_CROSS
        lines.append(f"{mark} <b>{name}:</b> <code>{v or 'not found'}</code>")

    try:
        import yt_dlp
        lines.append(f"{E_CHECK} <b>yt-dlp:</b> <code>{yt_dlp.version.__version__}</code>")
    except Exception:
        lines.append(f"{E_CROSS} <b>yt-dlp:</b> <code>not importable</code>")

    lines.append("")
    try:
        from playwright.async_api import async_playwright  # noqa: F401
        lines.append(f"{E_CHECK} <b>playwright (pip package):</b> installed")
    except ImportError:
        lines.append(f"{E_CROSS} <b>playwright (pip package):</b> not installed")
        lines.append(f"    <i>Run:</i> <code>pip install playwright</code>")
        await status.edit_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)
        return

    from Akbots.headless import system_chromium_path
    sys_chromium = system_chromium_path()
    if sys_chromium:
        lines.append(f"{E_CHECK} <b>chromium:</b> system binary at <code>{sys_chromium}</code>")
    else:
        cache_dir = os.path.expanduser("~/.cache/ms-playwright")
        found = glob.glob(os.path.join(cache_dir, "chromium-*"))
        if found:
            lines.append(f"{E_CHECK} <b>chromium:</b> self-installed at <code>{found[0]}</code>")
        else:
            lines.append(f"{E_CROSS} <b>chromium:</b> not found (neither system nor self-installed)")
            lines.append(f"    <i>Run:</i> <code>playwright install --with-deps chromium</code>")

    await status.edit_text("\n".join(lines), parse_mode=enums.ParseMode.HTML)
