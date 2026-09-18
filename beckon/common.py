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

# Live-session model names. settings.json can pick any of them; unknown names
# get the gemini-3.8-live shape (see model_caps).
LIVE_EXTENDED = "gemini-3.8-live-extended-thinking"
LIVE_FAST = "gemini-3.8-live"
LIVE_31 = "gemini-3.1-flash-live-preview"

# Every model name lives here, so the day one is deprecated there is one place
# to change -- and settings.json can override any of them without a code edit.
DEFAULTS = {
    "model": LIVE_EXTENDED,                        # the Live session
    "thinking_level": "medium",                    # used when the model requires it
    "voice": "Puck",
    "text_model": "gemini-3.8-flash",              # screen reads, find_on_screen
    "tts_model": "gemini-3.1-flash-tts-preview",   # tour narration
    "dev_url": "",     # tour's last stop; empty means the built-in default
    "dev_line": "",    # what the tour says over it
    "async_tools": "true",  # Phase 2: task-per-tool_call; "false" restores inline await
    "listen_indicator": "true",  # pulsing overlay while a Live session is up
}

# What each Live model accepts. live.py builds LiveConnectConfig from this;
# the panel uses it to show/hide thinking_level. Keep the keys in sync with
# the name constants above.
MODEL_CAPS = {
    LIVE_EXTENDED: {
        "thinking": "required",                 # low / medium / high; minimal → low
        "thinking_levels": ("low", "medium", "high"),
        "tools": "non_blocking",                # API rejects BLOCKING
        "end_of_turn": "idle",                  # interaction_status == IDLE
        "label": "3.8 Live Extended Thinking — thinks before it acts",
    },
    LIVE_FAST: {
        "thinking": "omit",                     # thinking_config must not be sent
        "thinking_levels": (),
        "tools": "either",                      # default NON_BLOCKING; BLOCKING allowed
        "end_of_turn": "turn_complete",
        "label": "3.8 Live — fast, no extended thinking",
    },
    LIVE_31: {
        "thinking": "optional",                 # omit unless a level is set
        "thinking_levels": ("minimal", "low", "medium", "high"),
        "tools": "sync",                        # no behavior= on declarations
        "end_of_turn": "turn_complete",
        "label": "3.1 Flash Live — previous default (sync tools)",
    },
}


def model_caps(model):
    """What a Live model accepts. Unknown names get the 3.8-live shape."""
    return MODEL_CAPS.get(model, MODEL_CAPS[LIVE_FAST])


def live_models():
    """Panel dropdown: id, one-line label, thinking levels (empty if none)."""
    return [{"id": name, "label": caps["label"],
             "thinking_levels": list(caps["thinking_levels"])}
            for name, caps in MODEL_CAPS.items()]


def normalize_thinking(model, level):
    """Thinking level to send, or None to omit thinking_config.

    required  — always send; blank → medium; minimal → low (API rejects it)
    optional  — send only when a level is given
    omit      — never send
    """
    caps = model_caps(model)
    kind = caps["thinking"]
    if kind == "omit":
        return None
    raw = (level or "").strip().lower()
    if raw == "minimal" and "minimal" not in caps["thinking_levels"]:
        raw = "low"
        # caller prints the warning; we just map
    if kind == "required":
        if raw not in caps["thinking_levels"]:
            raw = "medium" if "medium" in caps["thinking_levels"] else caps["thinking_levels"][0]
        return raw
    if raw in caps["thinking_levels"]:
        return raw
    return None


def thinking_was_mapped(model, level):
    """True when the caller asked for a level this model rejects (minimal→low)."""
    raw = (level or "").strip().lower()
    return raw == "minimal" and "minimal" not in model_caps(model)["thinking_levels"]


def validate_live_pair(model, thinking_level):
    """Error string if this model/thinking_level pair is invalid, else None.

    A blank thinking_level is always fine (required models fill medium later).
    A non-blank level is rejected when the model cannot take thinking_config.
    """
    if not model:
        return None
    caps = model_caps(model)
    level = (thinking_level or "").strip().lower()
    if not level:
        return None
    if caps["thinking"] == "omit":
        return f"{model} does not accept thinking_level"
    allowed = tuple(caps["thinking_levels"])
    if caps["thinking"] == "required":
        allowed = allowed + ("minimal",)
    if level not in allowed:
        return f"thinking_level must be {' / '.join(caps['thinking_levels'])}"
    return None


def resolved_thinking(model, explicit=None):
    """Thinking level to send, or None to omit thinking_config.

    None means the caller did not set one: required models get the DEFAULTS
    level, everyone else omits. An explicit blank string still maps required
    models to medium (normalize_thinking) so a stale empty setting cannot
    open an extended-thinking session without a level.
    """
    if explicit is None:
        if model_caps(model)["thinking"] == "required":
            explicit = DEFAULTS["thinking_level"]
        else:
            return None
    return normalize_thinking(model, explicit)


def raw_setting(key):
    """Value from settings.json only, or None if the key is unset/unreadable."""
    try:
        got = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        return None
    if isinstance(got, dict) and key in got:
        return got[key]
    return None


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


def setting_on(key):
    """True for 1/true/yes/on. A JSON false is not replaced by the default."""
    v = settings().get(key, DEFAULTS.get(key))
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


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
