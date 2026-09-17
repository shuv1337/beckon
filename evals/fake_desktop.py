"""A stand-in for Hyprland during eval runs.

FakeDesktop exposes a tool registry with exactly the shipped tools' names,
docstrings and signatures -- so the schema the model sees is byte-identical to
a real session -- but every body records the call and answers from a fixture
(evals/fixtures/desktops/*.json) instead of touching the machine.

Fixture keys: monitors, windows, active, installed, clipboard, page_text,
screen_answer, find, keybinds, memory, custom_tools, and optional "base" to
inherit from another fixture (shallow merge, child wins).
"""
import contextlib
import copy
import functools
import inspect
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "beckon"))

import memory   # noqa: E402
import tools    # noqa: E402

FIXTURES = ROOT / "evals" / "fixtures" / "desktops"
TERMINALS = ("alacritty", "kitty", "ghostty", "foot", "wezterm")


def load_fixture(name):
    d = json.loads((FIXTURES / f"{name}.json").read_text())
    base = d.pop("base", None)
    if base:
        merged = load_fixture(base)
        merged.update(d)
        return merged
    return d


class FakeDesktop:
    def __init__(self, fixture):
        if isinstance(fixture, str):
            fixture = load_fixture(fixture)
        self.fx = copy.deepcopy(fixture)
        self.windows = list(self.fx.get("windows", []))
        self.monitors = list(self.fx.get("monitors", []))
        self.active = self.fx.get("active")
        self.clipboard = self.fx.get("clipboard", "")
        self.keybinds = tools._parse_keybinds("\n".join(self.fx.get("keybinds", [])))
        self.pointer = (0, 0)
        self.calls = []          # [{"tool", "args", "result"}] in call order
        self.tools = self._build_registry()

    # ------------------------------------------------------------ registry

    def _build_registry(self):
        reg = {}
        for name, real in tools.BUILTIN_TOOLS.items():
            reg[name] = self._wrap(name, real, inspect.signature(real))
        for spec in self.fx.get("custom_tools", []):
            name = spec["name"]
            args = list(spec.get("args", []))
            sig = inspect.Signature([inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY) for a in args])

            def custom(**kw):
                return "done"
            custom.__name__ = name
            custom.__doc__ = spec.get("description", name)
            custom.__signature__ = sig
            reg[name] = self._wrap(name, custom, sig)
        return reg

    def _wrap(self, name, real, sig):
        handler = getattr(self, f"t_{name}", None)

        @functools.wraps(real)
        def fake(*a, **kw):
            bound = sig.bind(*a, **kw)
            bound.apply_defaults()
            args = dict(bound.arguments)
            result = handler(**args) if handler else "done"
            self.calls.append({"tool": name, "args": args, "result": result})
            return result
        return fake

    def tool_names(self):
        return [c["tool"] for c in self.calls]

    @contextlib.contextmanager
    def installed(self):
        """Point the memory module at a temp file seeded from the fixture, so
        remember/recall/forget and memory.render() see fixture state only."""
        old = memory.FILE
        with tempfile.TemporaryDirectory(prefix="beckon-eval-") as d:
            memory.FILE = Path(d) / "memory.json"
            mem = self.fx.get("memory") or {"preferences": {}, "notes": []}
            memory._save({"preferences": dict(mem.get("preferences", {})),
                          "notes": list(mem.get("notes", []))})
            try:
                yield self
            finally:
                memory.FILE = old

    # ------------------------------------------------------------ helpers

    def _win(self, address):
        return next((w for w in self.windows if w["address"] == str(address)), None)

    def _active(self):
        return self._win(self.active) or {}

    def _focused_monitor(self):
        return next((m for m in self.monitors if m.get("focused")), self.monitors[0] if self.monitors else {})

    def _need_active(self):
        return None if self.active else {"error": "no focused window"}

    # ------------------------------------------------------------ windows

    def t_list_windows(self):
        return [{"address": w["address"], "class": w["class"], "title": w["title"],
                 "workspace": w["workspace"]["name"], "monitor": w.get("monitor", 0),
                 "floating": w.get("floating", False)} for w in self.windows]

    def t_current_window(self):
        w = self._active()
        if not w:
            return {"error": "no focused window"}
        return {"address": w["address"], "class": w["class"], "title": w["title"],
                "workspace": w["workspace"]["name"]}

    def t_list_monitors(self):
        return [{"name": m["name"], "width": m["width"], "height": m["height"], "scale": m["scale"],
                 "x": m["x"], "y": m["y"], "focused": m.get("focused", False),
                 "workspace": m["activeWorkspace"]["name"]} for m in self.monitors]

    def t_focus_window(self, address):
        if not self._win(address):
            return {"error": f"no window with address {address}"}
        self.active = str(address)
        return "ok"

    def t_focus_direction(self, direction):
        if str(direction) not in ("l", "r", "u", "d"):
            return {"error": "direction must be l, r, u or d"}
        return "ok"

    def t_close_window(self):
        w = self._active()
        if not w:
            return {"error": "no focused window"}
        if tools._protected(w):
            return {"refused": f"won't close {w.get('class')} ({(w.get('title') or '')[:40]}) -- "
                               "that's the assistant or its panel; the user can close it themselves"}
        self.windows = [x for x in self.windows if x["address"] != w["address"]]
        self.active = self.windows[0]["address"] if self.windows else None
        return "ok"

    def t_toggle_float(self):
        return self._need_active() or "ok"

    def t_fullscreen(self, mode="fullscreen"):
        # the real tool coerces unknown modes to 'fullscreen' rather than erroring
        return self._need_active() or "ok"

    def t_move_window(self, direction):
        return self.t_focus_direction(direction)

    def t_swap_window(self, direction):
        return self.t_focus_direction(direction)

    def t_resize_window(self, x=0, y=0):
        return self._need_active() or "ok"

    def t_toggle_split(self):
        return self._need_active() or "ok"

    def t_switch_workspace(self, number):
        for m in self.monitors:
            if m.get("focused"):
                m["activeWorkspace"] = {"name": str(number)}
        return "ok"

    def t_move_to_workspace(self, number, follow=True):
        w = self._active()
        if not w:
            return {"error": "no focused window"}
        w["workspace"] = {"name": str(number)}
        if follow:
            self.t_switch_workspace(number)
        return "ok"

    def t_move_workspace_to_monitor(self, direction):
        return self.t_focus_direction(direction)

    def t_gather_windows(self, workspace=1):
        moved = 0
        for w in self.windows:
            if w["workspace"]["name"] != str(workspace):
                w["workspace"] = {"name": str(workspace)}
                moved += 1
        return {"moved": moved, "workspace": int(workspace)}

    # ------------------------------------------------------------ input

    def t_type_text(self, text):
        return f"typed {len(str(text))} chars"

    def t_press_key(self, key):
        return f"pressed {key}"

    def t_hotkey(self, combo):
        return f"pressed {combo}"

    def t_move_mouse(self, x, y):
        self.pointer = (int(x), int(y))
        return "ok"

    def t_click(self, button="left"):
        if str(button) not in ("left", "right", "middle"):
            return {"error": "button must be left, right or middle"}
        return f"{button} click at {self.pointer}"

    # ------------------------------------------------------------ system

    def t_read_clipboard(self):
        return self.clipboard or {"error": "clipboard is empty"}

    def t_set_clipboard(self, text):
        self.clipboard = str(text)
        return "ok"

    def _percent(self, percent, what):
        try:
            p = max(0, min(100, int(float(percent))))
        except (TypeError, ValueError):
            return {"error": "percent must be a number"}
        return f"{what} {p}%"

    def t_set_volume(self, percent):
        return self._percent(percent, "volume")

    def t_set_brightness(self, percent):
        return self._percent(percent, "brightness")

    def t_open_url(self, url):
        return f"opened {url}"

    def t_launch_app(self, command):
        first = str(command).split()[0] if str(command).split() else ""
        if first.lower() in TERMINALS or first.lower() == "terminal":
            return "opened the terminal"
        if first not in self.fx.get("installed", []):
            return {"error": f"'{first}' is not installed; ask the user what to use instead"}
        return f"launched {command}"

    def t_say(self, message):
        return "shown"

    def t_guided_tour(self, post_to_x=False):
        return {"status": "started", "post_to_x": bool(post_to_x)}

    # ------------------------------------------------------------ looking

    def t_look_at_screen(self, question="What is on the screen?", mode="auto"):
        mode = str(mode or "auto").lower()
        if mode not in ("auto", "image", "text"):
            mode = "auto"
        page = self.fx.get("page_text")
        if mode == "text":
            if page:
                return page
            return {"error": "that window publishes no readable text",
                    "hint": "nothing to read here; call look_at_screen with mode='image'"}
        answer = self.fx.get("screen_answer") or "(no answer)"
        if mode == "auto" and page:
            answer += "\n\nVisible text:\n" + page
        return answer

    def t_find_on_screen(self, description):
        want = str(description).lower()
        words = set(want.replace(",", " ").split())
        best, score = None, 0
        for key, pos in (self.fx.get("find") or {}).items():
            k = key.lower()
            s = 3 if (k in want or want in k) else len(words & set(k.split()))
            if s > score:
                best, score = pos, s
        if not best:
            return {"error": "could not locate it", "raw": f"nothing matching '{description}'"}
        return {"x": best["x"], "y": best["y"], "monitor": self._focused_monitor().get("name")}

    def t_read_page_text(self, window=""):
        page = self.fx.get("page_text")
        if not page:
            return {"error": "that window publishes no readable text",
                    "hint": "call look_at_screen with mode='image' instead"}
        return {"window": self._active().get("title"), "characters": len(page), "text": page}

    # ------------------------------------------------------------ keybinds

    def t_list_keybinds(self, query=""):
        q = str(query).lower().strip()
        rows = [b for b in self.keybinds if not q or q in b["name"].lower() or q in b["chord"].lower()]
        return [f"{b['chord']} -> {b['name']}" for b in rows[:40]] or ["no matches"]

    def t_press_keybind(self, name, force=False):
        hit = tools._match_keybind(name, self.keybinds)
        if not hit:
            return {"error": f"no shortcut named '{name}'", "hint": "try list_keybinds"}
        if hit["name"].lower() == "close window":
            return self.t_close_window()
        if tools._is_dangerous(hit["name"]) and not force:
            return {"refused": f"'{hit['name']}' needs explicit confirmation from the user (force=True)"}
        return f"pressed {hit['chord']} ({hit['name']})"

    # ------------------------------------------------------------ memory

    def t_remember(self, key, value):
        return memory.remember(key, value)

    def t_note(self, text):
        return memory.note(text)

    def t_recall(self, query=""):
        return memory.recall(query)

    def t_forget(self, target):
        return memory.forget(target)
