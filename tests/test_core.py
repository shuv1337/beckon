"""Unit tests for the pure parts of Beckon. Run with `pytest` from the repo root.

Nothing here touches Hyprland, audio, or the network; anything that would is
monkeypatched. live.py needs sounddevice and google-genai to import, so the
tests that cover it skip cleanly on a machine without them.
"""

import asyncio
import importlib
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

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


# ------------------------------------------------------------- live config / receive


import ui  # noqa: E402  — after live; save_settings lives here


def _thinking_name(config):
    tc = getattr(config, "thinking_config", None)
    if tc is None:
        return None
    level = getattr(tc, "thinking_level", None)
    return getattr(level, "name", None) or (str(level) if level is not None else None)


def _decl_behaviors(config):
    tools_ = getattr(config, "tools", None) or []
    first = tools_[0] if tools_ else None
    decls = getattr(first, "function_declarations", None) if first is not None else None
    if decls is None and isinstance(first, dict):
        decls = first.get("function_declarations")
    out = []
    for d in decls or []:
        if isinstance(d, dict):
            out.append((d.get("name"), d.get("behavior")))
        else:
            out.append((getattr(d, "name", None),
                        getattr(getattr(d, "behavior", None), "name",
                                getattr(d, "behavior", None))))
    return out


def _beh(b):
    s = str(b or "")
    if "NON_BLOCKING" in s:
        return "NON_BLOCKING"
    if "BLOCKING" in s:
        return "BLOCKING"
    return None


def test_live_config_follows_model_caps():
    # thinking=None uses resolved_thinking, not settings.json.
    decls = [{"name": "list_windows", "description": "list"},
             {"name": "click", "description": "click"}]
    for name, caps in common.MODEL_CAPS.items():
        cfg = live.live_config(model=name, thinking=None, voice="Puck",
                               known="", tool_decls=decls)
        level = _thinking_name(cfg)
        if caps["thinking"] == "required":
            assert level and "MEDIUM" in str(level).upper()
        else:
            assert level is None
        behaviors = {n: _beh(b) for n, b in _decl_behaviors(cfg)}
        if caps["tools"] == "sync":
            assert all(v is None for v in behaviors.values())
        elif caps["tools"] == "non_blocking":
            assert behaviors and all(v == "NON_BLOCKING" for v in behaviors.values())
        else:
            assert behaviors["list_windows"] == "NON_BLOCKING"
            assert behaviors["click"] == "BLOCKING"


def test_live_config_maps_minimal_to_low():
    cfg = live.live_config(model=common.LIVE_EXTENDED, thinking="minimal",
                           voice="Puck", known="", tool_decls=[])
    assert "LOW" in str(_thinking_name(cfg)).upper()


def test_live_config_unknown_model_is_fast_shape():
    cfg = live.live_config(model="gemini-no-such-model", thinking="medium",
                           voice="Puck", known="", tool_decls=[{"name": "x"}])
    assert _thinking_name(cfg) is None
    # 3.8-live shape: either → NON_BLOCKING unless @tool(blocking=True)
    assert all(_beh(b) == "NON_BLOCKING" for _, b in _decl_behaviors(cfg))


def test_turn_is_complete_idle_vs_filler():
    idle = SimpleNamespace(turn_complete=True, interaction_status="IDLE")
    filler = SimpleNamespace(turn_complete=True, interaction_status="IN_PROGRESS")
    legacy = SimpleNamespace(turn_complete=True)
    none = SimpleNamespace(turn_complete=False)
    assert live.turn_is_complete(idle) is True
    assert live.turn_is_complete(filler) is False
    assert live.turn_is_complete(legacy) is True
    assert live.turn_is_complete(none) is False
    assert live.turn_is_complete(None) is False


def test_validate_live_pair_and_resolved_thinking():
    assert common.validate_live_pair(common.LIVE_FAST, "medium")
    assert common.validate_live_pair(common.LIVE_FAST, "") is None
    assert common.validate_live_pair(common.LIVE_EXTENDED, "medium") is None
    assert common.validate_live_pair(common.LIVE_EXTENDED, "minimal") is None
    assert common.validate_live_pair(common.LIVE_EXTENDED, "banana")
    assert common.validate_live_pair(common.LIVE_31, "minimal") is None
    assert common.resolved_thinking(common.LIVE_EXTENDED, None) == "medium"
    assert common.resolved_thinking(common.LIVE_FAST, None) is None
    assert common.resolved_thinking(common.LIVE_FAST, "medium") is None
    assert common.normalize_thinking(common.LIVE_EXTENDED, "minimal") == "low"
    assert common.thinking_was_mapped(common.LIVE_EXTENDED, "minimal")
    assert not common.thinking_was_mapped(common.LIVE_31, "minimal")


def test_raw_setting_unset_vs_blank(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(common, "SETTINGS_FILE", f)
    assert common.raw_setting("thinking_level") is None
    f.write_text(json.dumps({"thinking_level": ""}))
    assert common.raw_setting("thinking_level") == ""
    assert common.setting("thinking_level") == common.DEFAULTS["thinking_level"]


def test_receive_filler_then_idle_is_one_turn(tmp_path, monkeypatch):
    hist = tmp_path / "history.jsonl"
    monkeypatch.setattr(live, "HISTORY", hist)
    monkeypatch.setattr(live, "log", lambda entry: hist.write_text(
        json.dumps({**entry, "ts": "t"}) + "\n"))
    monkeypatch.setattr(live, "run_tools",
                        lambda calls, registry=None, input_lock=None, skip_ids=None: [
                            {"result": {"ok": True}, "ms": 1, "ok": True}
                            for _ in calls])
    monkeypatch.setattr(live, "MUTE", tmp_path / "mute")

    class Sess:
        def __init__(self):
            self.sent = []

        async def send_tool_response(self, function_responses):
            self.sent.extend(function_responses)

    def sc(said, complete=False, status=None):
        return SimpleNamespace(
            input_transcription=None,
            output_transcription=SimpleNamespace(text=said),
            turn_complete=complete,
            interaction_status=status,
            interrupted=None,
        )

    def msg(said=None, complete=False, status=None, tool=None, cancel=None):
        tc = None
        if tool:
            tc = SimpleNamespace(function_calls=[
                SimpleNamespace(id="c1", name=tool, args={})])
        return SimpleNamespace(
            server_content=sc(said, complete, status) if said is not None else None,
            tool_call=tc,
            tool_call_cancellation=SimpleNamespace(ids=cancel) if cancel else None,
            usage_metadata=None,
            data=None,
        )

    sess = Sess()
    L = live.Live(once=True)

    async def go():
        await L.handle_message(sess, msg("checking that", True, "IN_PROGRESS"))
        await L.handle_message(sess, msg(tool="list_windows"))
        await L.handle_message(sess, msg("done", True, "IDLE"))

    asyncio.run(go())
    assert hist.exists()
    entry = json.loads(hist.read_text())
    assert "checking that" in entry["reply"] and "done" in entry["reply"]
    assert [a["tool"] for a in entry["actions"]] == ["list_windows"]
    assert sess.sent  # result was sent (not cancelled)


def test_cancelled_tool_ids_drop_results(tmp_path, monkeypatch):
    hist = tmp_path / "history.jsonl"
    monkeypatch.setattr(live, "HISTORY", hist)
    logged = []
    monkeypatch.setattr(live, "log", lambda entry: logged.append(entry))
    monkeypatch.setattr(live, "run_tools",
                        lambda calls, registry=None, input_lock=None, skip_ids=None: [
                            {"result": {"ok": True}, "ms": 1, "ok": True}
                            for _ in calls])
    monkeypatch.setattr(live, "MUTE", tmp_path / "mute")

    class Sess:
        def __init__(self):
            self.sent = []

        async def send_tool_response(self, function_responses):
            self.sent.extend(function_responses)

    def msg(said=None, complete=False, status=None, tool=None, cancel=None):
        tc = None
        if tool:
            tc = SimpleNamespace(function_calls=[
                SimpleNamespace(id="c1", name=tool, args={})])
        sc = None
        if said is not None:
            sc = SimpleNamespace(
                input_transcription=None,
                output_transcription=SimpleNamespace(text=said),
                turn_complete=complete,
                interaction_status=status,
                interrupted=None,
            )
        return SimpleNamespace(
            server_content=sc, tool_call=tc,
            tool_call_cancellation=SimpleNamespace(ids=cancel) if cancel else None,
            usage_metadata=None, data=None,
        )

    sess = Sess()
    L = live.Live()

    async def go():
        await L.handle_message(sess, msg(cancel=["c1"]))
        await L.handle_message(sess, msg(tool="list_windows"))
        await L.handle_message(sess, msg("ok", True, "IDLE"))

    asyncio.run(go())
    assert sess.sent == []
    assert logged and logged[0]["actions"] == []  # skipped, not run


def test_save_settings_rejects_thinking_for_fast(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(common, "SETTINGS_FILE", f)
    monkeypatch.setattr(ui, "DEFAULTS", common.DEFAULTS)
    with pytest.raises(ValueError, match="does not accept thinking_level"):
        ui.save_settings({"model": common.LIVE_FAST, "thinking_level": "medium"})
    ui.save_settings({"model": common.LIVE_FAST, "thinking_level": ""})
    saved = json.loads(f.read_text())
    assert saved["model"] == common.LIVE_FAST
    assert saved.get("thinking_level") == ""
    ui.save_settings({"model": common.LIVE_EXTENDED, "thinking_level": "high"})
    ui.save_settings({"model": common.LIVE_FAST})  # key absent → clear leftover
    saved = json.loads(f.read_text())
    assert saved["thinking_level"] == ""
    ui.save_settings({"listen_indicator": "false", "async_tools": "true"})
    saved = json.loads(f.read_text())
    assert saved["listen_indicator"] == "false"
    assert saved["async_tools"] == "true"


def test_tool_response_puts_scheduling_in_payload_not_field():
    call = SimpleNamespace(id="c1", name="list_windows")
    row = {"result": {"ok": True}, "ok": True, "fn": tools.list_windows}
    fr = live.tool_response(call, row)
    assert fr.response["scheduling"] == "INTERRUPT"
    assert getattr(fr, "scheduling", None) is None


def test_scheduling_per_tool_interrupt_on_error():
    assert live.scheduling_for(tools.list_windows, True) == "INTERRUPT"
    assert live.scheduling_for(tools.click, True) == "SILENT"
    assert live.scheduling_for(tools.click, False) == "INTERRUPT"
    custom = SimpleNamespace(_beckon_scheduling="WHEN_IDLE")
    assert live.scheduling_for(custom, True) == "WHEN_IDLE"
    assert live.scheduling_for(custom, False) == "INTERRUPT"
    assert live.scheduling_for(None, True) == "SILENT"


def test_declarations_have_no_behavior():
    for d in live.declarations(tools.BUILTIN_TOOLS):
        assert "behavior" not in d


def test_click_is_blocking_only_on_fast():
    decls = [{"name": "click", "description": "c"},
             {"name": "press_keybind", "description": "k"}]
    fast = live.stamp_behavior(decls, common.LIVE_FAST)
    ext = live.stamp_behavior(decls, common.LIVE_EXTENDED)
    old = live.stamp_behavior(decls, common.LIVE_FAST, async_tools=False)
    assert {d["name"]: d.get("behavior") for d in fast} == {
        "click": "BLOCKING", "press_keybind": "BLOCKING"}
    assert all(d.get("behavior") == "NON_BLOCKING" for d in ext)
    assert all("behavior" not in d for d in old)


def test_cancelled_still_runs_when_async_off(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr(live, "log", lambda entry: logged.append(entry))
    monkeypatch.setattr(live, "run_tools",
                        lambda calls, registry=None, input_lock=None, skip_ids=None: [
                            {"result": {"ok": True}, "ms": 1, "ok": True}
                            for _ in calls])
    monkeypatch.setattr(live, "MUTE", tmp_path / "mute")

    class Sess:
        def __init__(self):
            self.sent = []

        async def send_tool_response(self, function_responses):
            self.sent.extend(function_responses)

    def msg(said=None, complete=False, status=None, tool=None, cancel=None):
        tc = None
        if tool:
            tc = SimpleNamespace(function_calls=[
                SimpleNamespace(id="c1", name=tool, args={})])
        sc = None
        if said is not None:
            sc = SimpleNamespace(
                input_transcription=None,
                output_transcription=SimpleNamespace(text=said),
                turn_complete=complete, interaction_status=status, interrupted=None)
        return SimpleNamespace(
            server_content=sc, tool_call=tc,
            tool_call_cancellation=SimpleNamespace(ids=cancel) if cancel else None,
            usage_metadata=None, data=None)

    sess = Sess()
    L = live.Live(async_tools=False)

    async def go():
        await L.handle_message(sess, msg(cancel=["c1"]))
        await L.handle_message(sess, msg(tool="list_windows"))
        await L.handle_message(sess, msg("ok", True, "IDLE"))

    asyncio.run(go())
    assert sess.sent == []
    assert logged and [a["tool"] for a in logged[0]["actions"]] == ["list_windows"]


def test_input_tools_serialize_across_messages():
    order, lock = [], threading.Lock()

    def move_mouse(**kw):
        order.append("move-start")
        time.sleep(0.05)
        order.append("move-end")
        return "ok"

    def click(**kw):
        order.append("click")
        return "ok"

    def call(name):
        return SimpleNamespace(id=name, name=name, args={})

    def go(name):
        live.run_tools([call(name)], registry={"move_mouse": move_mouse, "click": click},
                       input_lock=lock)

    t1 = threading.Thread(target=go, args=("move_mouse",))
    t2 = threading.Thread(target=go, args=("click",))
    t1.start()
    time.sleep(0.01)
    t2.start()
    t1.join()
    t2.join()
    assert order == ["move-start", "move-end", "click"]


def test_listen_indicator_off_starts_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"listen_indicator": "false"}))
    assert live.start_listen_indicator() is None
    assert (Path(__file__).resolve().parents[1] / "beckon" / "listen.qml").is_file()


def test_setting_on_false_not_overlaid(tmp_path, monkeypatch):
    f = tmp_path / "settings.json"
    monkeypatch.setattr(common, "SETTINGS_FILE", f)
    f.write_text(json.dumps({"async_tools": False}))
    assert common.setting_on("async_tools") is False
    f.write_text(json.dumps({"async_tools": "false"}))
    assert common.setting_on("async_tools") is False
    f.write_text("{}")
    assert common.setting_on("async_tools") is True
