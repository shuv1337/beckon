"""Offline guards for the eval harness: the fake desktop presents the shipped
tool schema, sandboxes memory and refuses what the real tools refuse; the case
file is well-formed; the scorer's matchers behave. No network."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "beckon"))

from evals import score                                   # noqa: E402
from evals.fake_desktop import FIXTURES, FakeDesktop, load_fixture   # noqa: E402
import memory                                             # noqa: E402
import tools                                              # noqa: E402

live = pytest.importorskip("live")   # needs google-genai + sounddevice


def test_fixture_inheritance_child_wins():
    fx = load_fixture("gmail_reading")
    assert fx["page_text"].startswith("From: Dana")
    assert fx["windows"] == load_fixture("two_windows")["windows"]   # inherited
    assert "base" not in fx


def test_fake_schema_matches_shipped_tools():
    fd = FakeDesktop("two_windows")
    real = live.declarations(tools.BUILTIN_TOOLS)
    fake = [d for d in live.declarations(fd.tools) if d["name"] not in {"deploy_site"}]
    assert real == fake
    assert "deploy_site" in fd.tools                     # fixture custom tool declared


def test_fake_records_calls_with_defaults_bound():
    fd = FakeDesktop("two_windows")
    fd.tools["move_to_workspace"](number=3)
    fd.tools["press_keybind"]("screenshot")
    assert fd.calls[0] == {"tool": "move_to_workspace", "args": {"number": 3, "follow": True},
                           "result": "ok"}
    assert fd.calls[1]["args"] == {"name": "screenshot", "force": False}
    assert fd.calls[1]["result"].startswith("pressed SUPER SHIFT + S")


def test_fake_refuses_what_real_tools_refuse():
    fd = FakeDesktop("two_windows")
    assert "refused" in fd.tools["press_keybind"]("lock")
    assert fd.tools["press_keybind"]("lock", force=True).startswith("pressed")
    assert "error" in fd.tools["press_keybind"]("no such thing")
    assert "error" in fd.tools["launch_app"]("discord")
    assert fd.tools["launch_app"]("alacritty") == "opened the terminal"
    fd.windows.append({"address": "0x9", "class": "chromium", "title": "Beckon",
                       "workspace": {"name": "1"}})
    fd.active = "0x9"
    assert "refused" in fd.tools["close_window"]()


def test_fake_memory_is_sandboxed(tmp_path):
    real_file = memory.FILE
    fd = FakeDesktop("with_preferences")
    with fd.installed():
        assert memory.FILE != real_file
        assert "email: https://mail.google.com" in memory.render()
        assert fd.tools["remember"]("editor", "cursor") == "remembered editor = cursor"
        assert memory.recall("editor")["preferences"]["editor"] == "cursor"
    assert memory.FILE == real_file
    with FakeDesktop("two_windows").installed():
        assert memory.render() == ""                      # empty fixture -> no block


def test_fake_find_and_page_text():
    fd = FakeDesktop("gmail_reading")
    assert fd.tools["find_on_screen"]("the first email in the inbox") == \
        {"x": 420, "y": 260, "monitor": "eDP-1"}
    assert "error" in fd.tools["find_on_screen"]("a unicorn")
    assert fd.tools["read_page_text"]()["characters"] > 100
    assert "error" in FakeDesktop("terminal_focused").tools["read_page_text"]()
    assert "Visible text" in fd.tools["look_at_screen"]("what is this", "auto")


def test_cases_file_is_well_formed():
    lines = (ROOT / "evals" / "cases.jsonl").read_text().splitlines()
    cases = [json.loads(l) for l in lines if l.strip()]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    fixtures = {p.stem for p in FIXTURES.glob("*.json")}
    registries = {}
    for c in cases:
        names = registries.setdefault(c["desktop"], set(FakeDesktop(c["desktop"]).tools))
        assert score.validate_case(c, fixtures, names) == [], c["id"]
    assert len(cases) >= 60


def test_validate_case_reports_problems():
    assert score.validate_case({"id": "x"}) == ["missing cat", "missing say", "missing desktop",
                                                 "missing expect"]
    bad = {"id": "x", "cat": "c", "say": "s", "desktop": "nope",
           "expect": {"bogus": 1, "forbid": ["not_a_tool"]}}
    got = score.validate_case(bad, {"two_windows"}, {"click"})
    assert "unknown expect key bogus" in got
    assert "unknown desktop nope" in got
    assert "forbid: unknown tool not_a_tool" in got


def test_match_value_matchers():
    m = score.match_value
    assert m("Return", "return") and not m("Return", "enter")
    assert m(3, "3") and m(3, 3.0) and not m(3, "x")
    assert m(True, True) and not m(True, "true")         # coerce_args' job, not ours
    assert m(">0", 40) and not m(">0", 0) and not m(">0", "wide")
    assert m("~mail", "https://mail.google.com") and not m("~mail", "https://x.com")
    assert m("re:^(return|enter)$", "ENTER") and not m("re:^(return|enter)$", "enter key")


def test_score_ordered_is_a_subsequence():
    case = {"expect": {"tools_ordered": ["find_on_screen", "move_mouse", "click"]}}
    calls = [{"tool": t, "args": {}} for t in
             ["list_windows", "find_on_screen", "current_window", "move_mouse", "click"]]
    assert score.score(case, calls, ["done"])["pass"]
    calls = [{"tool": t, "args": {}} for t in ["move_mouse", "find_on_screen", "click"]]
    assert not score.score(case, calls, ["done"])["pass"]


def test_score_args_forbid_question_words():
    case = {"expect": {"tools_any_order": ["move_to_workspace"],
                       "args": {"move_to_workspace": {"number": 4, "follow": False}},
                       "forbid": ["close_window"], "forbid_question": True,
                       "reply_max_words": 5}}
    ok = [{"tool": "move_to_workspace", "args": {"number": 4, "follow": False}}]
    assert score.score(case, ok, ["Done."]) == {"pass": True, "failed": []}
    r = score.score(case, [{"tool": "move_to_workspace", "args": {"number": 4, "follow": True}},
                           {"tool": "close_window", "args": {}}],
                    ["Should I close it too? Sure thing, all done now."])
    assert {f.split(":")[0] for f in r["failed"]} == \
        {"args.move_to_workspace", "forbid", "forbid_question", "reply_max_words"}


def test_score_first_turn_and_no_tools():
    case = {"expect": {"forbid_first_turn": ["open_url"], "expect_question": True,
                       "tools_any_order": ["remember", "open_url"]}}
    calls = [{"tool": "remember", "args": {}}, {"tool": "open_url", "args": {}}]
    assert score.score(case, calls, ["Which email do you use?", "Opening Gmail."], [])["pass"]
    assert not score.score(case, calls, ["Opening.", "ok"], ["open_url"])["pass"]
    assert score.score({"expect": {"no_tools": True}}, [], ["bye"])["pass"]
    assert not score.score({"expect": {"no_tools": True}}, calls[:1], ["bye"])["pass"]


def test_summarize_rates_and_telemetry():
    rows = [{"cat": "a", "pass": True, "failed": [], "turn_ms": 1000, "usage": {"total": 10},
             "reply": "one two", "calls": [{"ok": True}]},
            {"cat": "a", "pass": False, "failed": ["forbid: called [x]"], "turn_ms": 3000,
             "usage": {"total": 30}, "reply": "", "calls": [{"ok": False}], "timeout": True},
            {"cat": "b", "pass": True, "failed": [], "turn_ms": 2000, "usage": None,
             "reply": "x", "calls": []}]
    s = score.summarize(rows)
    assert s["pass_rate"] == 67 and s["by_cat"]["a"]["pass_rate"] == 50
    assert s["forbid_violations"] == 1 and s["timeouts"] == 1 and s["tool_failures"] == 1
    assert s["avg_turn_ms"] == 2000 and s["total_tokens"] == 40 and s["avg_reply_words"] == 1.0


def test_audio_readback_matcher():
    from evals.run import matches_utterance as m
    assert not m("lock the screen", "Walk the screen.")        # the real mis-render we hit
    assert m("lock the screen", "Lock the screen")
    assert not m("set the volume to fifty percent", "Set the volume to 50%")
    assert m("thanks, that's all", "Thanks. That's all.")
    assert m("fullscreen this", "Full screen this.")          # compound split is faithful
    assert m("open the first email and read it to me", "Open the first email and read to me")
    assert not m("open the first email and read it to me", "Open the email")


def test_live_registry_seams():
    fd = FakeDesktop("two_windows")

    class Call:
        def __init__(self, name, args):
            self.name, self.args = name, args

    res = live.run_tools([Call("set_volume", {"percent": "40"}), Call("press_keybind",
                          {"name": "lock", "force": "false"}), Call("nope", {})], fd.tools)
    assert [r["ok"] for r in res] == [True, False, False]
    assert res[0]["result"] == "volume 40%"
    assert fd.calls[1]["args"]["force"] is False           # "false" never reads as True
    assert tools.TOOLS is not fd.tools                     # real registry untouched
