"""Shared plumbing: paths, settings, the API key, Gemini HTTP, ydotool, browser.

A leaf module -- it imports nothing else from Beckon -- so live.py, tools.py,
tour.py, narrate.py and ui.py can all use it without import cycles. Anything
that used to be copied between those files lives here once.
"""

import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

CONFIG = Path.home() / ".config" / "beckon"
DATA = Path.home() / ".local" / "share" / "beckon"
STATE = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "beckon"
KEYFILE = CONFIG / "api_key"
SETTINGS_FILE = CONFIG / "settings.json"

# Every model name lives here, so the day one is deprecated there is one place
# to change -- and settings.json can override any of them without a code edit.
DEFAULTS = {
    "model": "gemini-3.1-flash-live-preview",     # the Live session
    "voice": "Puck",
    "text_model": "gemini-3.8-flash",              # screen reads, find_on_screen
    "tts_model": "gemini-3.1-flash-tts-preview",   # tour narration
    "dev_url": "",     # tour's last stop; empty means the built-in default
    "dev_line": "",    # what the tour says over it
}


def settings():
    """DEFAULTS overlaid with ~/.config/beckon/settings.json. Unknown keys and
    unreadable JSON are ignored rather than fatal."""
    s = dict(DEFAULTS)
    try:
        got = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return s
    if isinstance(got, dict):
        s.update({k: v for k, v in got.items() if k in DEFAULTS})
    return s


def setting(key):
    """One setting; the default when it is unset or blank."""
    return settings().get(key) or DEFAULTS.get(key)


def api_key():
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if k:
        return k.strip()
    try:
        return KEYFILE.read_text().strip() or None
    except OSError:
        return None


def write_private(path, text):
    """Write a file that is 0600 from its very first byte, then swap it into
    place atomically -- so a key or a memory file is never briefly world-readable
    and never half-written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")   # 0600 by design
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"


def generate_content(model, body, timeout=45):
    """POST to generateContent and return the parsed JSON. The key travels in a
    header, not the query string, so it never lands in a proxy or server log.
    Raises on any failure; callers decide how to report it."""
    key = api_key()
    if not key:
        raise RuntimeError("no API key")
    req = urllib.request.Request(
        f"{GEMINI}/{model}:generateContent", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def answer_text(data):
    """The text parts of a generateContent response, joined."""
    parts = data["candidates"][0]["content"]["parts"]
    return " ".join(p.get("text", "") for p in parts).strip()


def ydotool_socket():
    """$YDOTOOL_SOCKET, else the runtime-dir socket, else the /tmp one the README's
    system unit creates."""
    sock = os.environ.get("YDOTOOL_SOCKET")
    if sock:
        return sock
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / ".ydotool_socket"
    return str(runtime if runtime.exists() else Path("/tmp/.ydotool_socket"))


def ydo(*args):
    env = dict(os.environ, YDOTOOL_SOCKET=ydotool_socket())
    return subprocess.run(["ydotool", *args], capture_output=True, text=True, timeout=5, env=env)


def browser():
    """The first Chromium-family browser on PATH. Chromium-family because the
    panel is opened with --app= and the tour drives it over MPRIS."""
    for cmd in ("google-chrome-stable", "google-chrome", "chromium", "brave"):
        if shutil.which(cmd):
            return cmd
    return "chromium"


def notify(msg, urgency="normal", title="Beckon", ms=None):
    cmd = ["notify-send", "-a", "Beckon", "-u", urgency]
    if ms:
        cmd += ["-t", str(int(ms))]
    subprocess.run(cmd + [title, str(msg)[:250]], check=False)
