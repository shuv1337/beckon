"""Unit tests for the pure parts of Beckon. Run with `pytest` from the repo root.

Nothing here touches Hyprland, audio, or the network; anything that would is
monkeypatched. live.py needs sounddevice and google-genai to import, so the
tests that cover it skip cleanly on a machine without them.
"""

import importlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "beckon"))

import common  # noqa: E402
import memory  # noqa: E402
import tools   # noqa: E402
import tour    # noqa: E402

live = pytest.importorskip("live", reason="sounddevice / google-genai not installed")


# ------------------------------------------------------------- tool schema

def _ptype(name, param):
    decl = {d["name"]: d for d in live.declarations()}
    return decl[name]["parameters"]["properties"][param]["type"]


def test_flags_are_declared_boolean():
    # A bool default is a subclass of int; declaring it STRING once made
    # bool("false") read as True and bypassed the force= guard.
    assert _ptype("press_keybind", "force") == "BOOLEAN"
    assert _ptype("move_to_workspace", "follow") == "BOOLEAN"
    assert _ptype("guided_tour", "post_to_x") == "BOOLEAN"


def test_ints_and_strings_keep_their_types():
    assert _ptype("resize_window", "x") == "INTEGER"
    assert _ptype("gather_windows", "workspace") == "INTEGER"
    assert _ptype("look_at_screen", "mode") == "STRING"


def test_required_lists_only_defaultless_params():
    decl = {d["name"]: d for d in live.declarations()}
    assert decl["press_keybind"]["parameters"]["required"] == ["name"]
    assert "parameters" not in decl["list_windows"]


@pytest.mark.parametrize("raw,expected", [
    ("false", False), ("False", False), ("0", False), ("no", False), ("", False),
    ("true", True), ("1", True), ("yes", True), (True, True), (False, False),
])
def test_coerce_bool_strings(raw, expected):
    assert live.coerce_args(tools.press_keybind, {"name": "x", "force": raw})["force"] is expected


def test_coerce_ints_and_leaves_garbage_for_the_tool():
    assert live.coerce_args(tools.resize_window, {"x": "150", "y": 2.0}) == {"x": 150, "y": 2}
    assert live.coerce_args(tools.gather_windows, {"workspace": "abc"}) == {"workspace": "abc"}


# ------------------------------------------------------------- keybind guard

@pytest.mark.parametrize("name", ["Lock system", "Power", "Power menu", "Close all windows", "Log out", "Reboot"])
def test_dangerous_binds_need_force(name):
    assert tools._is_dangerous(name)


@pytest.mark.parametrize("name", ["Restart Waybar", "Toggle locking on idle", "Close window", "Unlock bitwarden"])
def test_ordinary_binds_are_not_dangerous(name):
    assert not tools._is_dangerous(name)


def test_keybind_table_parses(monkeypatch):
    class R:
        stdout = ("SUPER + Return                      → Terminal\n"
                  "SUPER SHIFT + S                     → Screenshot\n"
                  "XF86AudioMute                       → Mute\n"
                  "garbage line without an arrow\n")
    monkeypatch.setattr(tools.subprocess, "run", lambda *a, **k: R())
    rows = tools._keybinds()
    assert [r["name"] for r in rows] == ["Terminal", "Screenshot", "Mute"]
    assert rows[1]["mods"] == ["SUPER", "SHIFT"] and rows[1]["key"] == "S"
    assert rows[2]["mods"] == [] and rows[2]["key"] == "XF86AudioMute"


# ------------------------------------------------------------- private files

def test_write_private_is_0600_atomic_and_tidy(tmp_path):
    p = tmp_path / "sub" / "secret"
    common.write_private(p, "hello")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert p.read_text() == "hello"
    common.write_private(p, "again")
    assert p.read_text() == "again"
    assert os.listdir(p.parent) == ["secret"]        # no temp file left behind


def test_memory_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "FILE", tmp_path / "memory.json")
    memory.remember("email", "gmail")
    memory.note("it's a 'quoted' note")
    assert memory.recall("email")["preferences"] == {"email": "gmail"}
    assert "quoted" in memory.recall("quoted")["notes"][0]
    assert stat.S_IMODE(os.stat(memory.FILE).st_mode) == 0o600
    assert memory.forget("email") == "forgot 1 item(s)"
    assert memory.recall()["preferences"] == {}


def test_memory_caps_hold(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "FILE", tmp_path / "memory.json")
    for i in range(memory.MAX_NOTES + 10):
        memory.note(f"note {i}")
    assert len(memory._load()["notes"]) == memory.MAX_NOTES
    memory.remember("k", "x" * 1000)
    assert len(memory._load()["preferences"]["k"]) == memory.MAX_LEN


# ------------------------------------------------------------- settings

def test_settings_ignore_unknown_keys_and_bad_json(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(common, "SETTINGS_FILE", f)
    assert common.settings() == common.DEFAULTS
    f.write_text(json.dumps({"voice": "Kore", "evil": 1, "dev_url": "http://localhost:5173"}))
    s = common.settings()
    assert s["voice"] == "Kore" and s["dev_url"] == "http://localhost:5173" and "evil" not in s
    f.write_text("{not json")
    assert common.settings() == common.DEFAULTS


# ------------------------------------------------------------- custom tools

def test_custom_tools_load_and_quote(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "CUSTOM_FILE", tmp_path / "custom_tools.json")
    tools.CUSTOM_FILE.write_text(json.dumps([
        {"name": "echo_it", "description": "Echo", "command": "echo {what}", "args": ["what"]},
        {"name": "list_windows", "command": "true"},          # shadows a builtin: dropped
        {"name": "bad name", "command": "true"},              # not an identifier: dropped
    ]))
    got = tools.load_custom_tools()
    assert list(got) == ["echo_it"]
    assert got["echo_it"](what="a'b; rm -rf /") == "a'b; rm -rf /"   # quoted, not executed


# ------------------------------------------------------------- tour

def test_tour_running_flag_survives_reload():
    ev = tour._running
    ev.set()
    try:
        importlib.reload(tour)
        assert tour._running is ev and tour._running.is_set()
    finally:
        ev.clear()
