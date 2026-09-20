#!/usr/bin/env python3
"""Local control panel for Beckon.

Serves a single-page UI on 127.0.0.1 only. Handles the API key, settings, voice
selection and testing, the tool list, and the conversation history -- so none of
it has to be edited by hand.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import common    # noqa: E402
import tools     # noqa: E402
import memory    # noqa: E402

CONFIG = common.CONFIG
KEYFILE = common.KEYFILE
HISTORY = common.DATA / "history.jsonl"
CUSTOM = CONFIG / "custom_tools.json"


def memory_view():
    m = memory._load(); st = memory.status()
    return {"preferences": m["preferences"], "notes": m["notes"], "bytes": st["bytes"], "caps": st["caps"]}


def read_custom():
    try:
        return json.loads(CUSTOM.read_text())
    except (OSError, json.JSONDecodeError):
        return []


def write_custom(items):
    import importlib
    common.write_private(CUSTOM, json.dumps(items, indent=2))   # shell commands the user wrote
    importlib.reload(tools)   # so the panel's tool list reflects it immediately

PORT = int(os.environ.get("BECKON_UI_PORT", "8777"))

DEFAULTS = common.DEFAULTS
load_settings = common.settings


def save_settings(new):
    """Merge known keys into settings.json. Every key in common.DEFAULTS is
    settable here -- including dev_url/dev_line, which the README documents.

    Switching to a model that omits thinking_config clears a stored level so
    the next session does not inherit a leftover medium. A non-blank level
    sent for such a model is kept so validate_live_pair can reject it.
    """
    s = load_settings()
    s.update({k: str(v).strip() for k, v in new.items() if k in DEFAULTS and isinstance(v, (str, int, float))})
    model = s.get("model") or ""
    if common.model_caps(model)["thinking"] == "omit" and "thinking_level" not in new:
        s["thinking_level"] = ""
    err = common.validate_live_pair(model, s.get("thinking_level"))
    if err:
        raise ValueError(err)
    common.write_private(common.SETTINGS_FILE, json.dumps(s, indent=2))
    return s


LIVE_VOICES = [
    ("Puck", "bright, upbeat"),
    ("Charon", "deep, measured"),
    ("Kore", "warm, even"),
    ("Fenrir", "gravelly"),
    ("Aoede", "light, airy"),
    ("Leda", "youthful"),
    ("Orus", "firm"),
    ("Zephyr", "soft"),
]


def list_voices():
    """Gemini Live prebuilt voices -- synthesis happens server-side now."""
    return [{"id": n, "name": f"{n} — {d}", "engine": "gemini"} for n, d in LIVE_VOICES]


def live_running():
    """True if the PID file names a live Beckon session -- not just any process
    that inherited a stale PID."""
    pidfile = common.STATE / "live.pid"
    try:
        pid = int(pidfile.read_text().strip())
        return b"live.py" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return False


def key_status():
    if not KEYFILE.exists():
        return {"set": False, "bytes": 0, "hint": ""}
    raw = KEYFILE.read_text().strip()
    return {
        "set": len(raw) > 20,
        "bytes": len(raw),
        # the key is never echoed back, not even partially
        "hint": "",
        "looks_like_gemini": raw.startswith("AIza"),
    }


def recent_history(n=40):
    if not HISTORY.exists():
        return []
    lines = HISTORY.read_text().splitlines()[-n:]
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # keep the terminal quiet

    def _send(self, obj, code=200, ctype="application/json"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            html = (HERE / "ui.html").read_bytes()
            return self._send(html, ctype="text/html; charset=utf-8")
        if self.path == "/api/state":
            s = load_settings()
            return self._send({
                "key": key_status(),
                "settings": s,
                "voices": list_voices(),
                "models": common.live_models(),
                "thinking_raw": common.raw_setting("thinking_level"),
                "active_voice": s["voice"],
                "live_running": live_running(),
                "tools": [
                    {"name": n, "doc": (f.__doc__ or "").strip()}
                    for n, f in sorted(tools.TOOLS.items())
                ],
                "history": recent_history(),
                "custom_tools": read_custom(),
                "memory": memory_view(),
                "windows": tools.list_windows(),
                "monitors": tools.list_monitors(),
            })
        return self._send({"error": "not found"}, 404)

    def _same_origin(self):
        """Only the panel's own page may POST. Any site the user has open can
        fire a text/plain POST at 127.0.0.1:8777 without a preflight, and one
        of these endpoints registers a shell command the assistant can run --
        so require a JSON content type, a Host that is really us (DNS
        rebinding), and an Origin that is us or absent (same-origin fetches
        from this page send our own origin; cross-site ones send theirs)."""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if host not in allowed:
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin not in {f"http://{h}" for h in allowed}:
            return False
        return True

    def do_POST(self):
        if not self._same_origin():
            return self._send({"error": "cross-origin request refused"}, 403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send({"error": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._send({"error": "bad json"}, 400)

        if self.path == "/api/key":
            key = (body.get("key") or "").strip()
            if len(key) < 20:
                return self._send({"error": "that doesn't look like a key (too short)"}, 400)
            common.write_private(KEYFILE, key)   # 0600 from the first byte, atomic
            return self._send({"ok": True, "key": key_status()})

        if self.path == "/api/settings":
            try:
                return self._send({"ok": True, "settings": save_settings(body)})
            except ValueError as e:
                return self._send({"error": str(e)}, 400)

        if self.path == "/api/custom-tools":
            items = read_custom()
            if "remove" in body:
                items = [t for t in items if t.get("name") != body["remove"]]
                write_custom(items)
                return self._send({"ok": True, "custom_tools": items})
            add = body.get("add") or {}
            name = str(add.get("name", "")).strip()
            builtin = set(tools.TOOLS) - {t.get("name") for t in items}
            if not name.isidentifier():
                return self._send({"error": "name must be letters, digits and underscores"}, 400)
            if name in builtin:
                return self._send({"error": f"'{name}' is a built-in tool"}, 400)
            if not str(add.get("command", "")).strip():
                return self._send({"error": "command is required"}, 400)
            args = [a for a in add.get("args", []) if str(a).isidentifier()]
            items = [t for t in items if t.get("name") != name] + [{
                "name": name,
                "description": str(add.get("description", "")).strip() or name,
                "command": str(add["command"]).strip(),
                "args": args,
            }]
            write_custom(items)
            return self._send({"ok": True, "custom_tools": items})

        if self.path == "/api/memory":
            if body.get("clear"):
                memory.clear()
            elif body.get("forget"):
                memory.forget(body["forget"])
            return self._send({"ok": True, "memory": memory_view()})

        if self.path == "/api/clear-history":
            common.write_private(HISTORY, "")
            return self._send({"ok": True})

        if self.path == "/api/live":
            action = body.get("action")
            # live.py reads settings.json itself now; env is kept so a panel
            # start still wins over a stale env in the panel's own process.
            st = load_settings()
            env = dict(os.environ, BECKON_LIVE_VOICE=st["voice"], BECKON_LIVE_MODEL=st["model"])
            raw = common.raw_setting("thinking_level")
            if raw is not None:
                env["BECKON_LIVE_THINKING"] = str(raw)
            else:
                env.pop("BECKON_LIVE_THINKING", None)
            # live.py toggles: with a session running it stops it, otherwise it starts one
            if (action == "start") != live_running():
                subprocess.Popen([sys.executable, str(HERE / "live.py")],
                                 env=env, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import time as _t
            _t.sleep(1.2)
            return self._send({"ok": True, "running": live_running()})

        return self._send({"error": "not found"}, 404)


def main():
    srv = HTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}/"
    print(f"Beckon control panel -> {url}   (Ctrl+C to stop)")
    if "--no-open" not in sys.argv:
        threading.Timer(0.6, lambda: subprocess.run(
            ["uwsm-app", "--", common.browser(), f"--app={url}"],
            check=False)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
