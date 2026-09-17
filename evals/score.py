"""Score one eval case against what the model actually did.

A case (evals/cases.jsonl) has:
  id, cat, say, desktop            -- required
  then: [..]                       -- optional follow-up utterances (same session)
  expect: {...}                    -- checks; a case passes when every check passes

expect keys:
  tools_ordered   [names]   these appear in this order (other calls may sit between)
  tools_any_order [names]   each appears at least once
  tools_any_of    [names]   at least one appears
  forbid          [names]   none of these appear
  forbid_first_turn [names] none of these appear before the first reply
  no_tools        true      no tool call at all
  max_tool_calls  N         at most N calls
  args            {tool: {arg: matcher}}   some call to `tool` matches every arg
  expect_question true      the first reply contains a question
  forbid_question true      no reply contains a question
  reply_max_words N         the last reply has at most N words
  reply_mentions_any [..]   some reply contains one of these (case-insensitive)
  reply_mentions_all [..]   every one of these appears in some reply
  reply_forbids   [..]      none of these appear in any reply

arg matchers: a literal (case-insensitive for strings, numeric for numbers,
bool for bools) or a string starting with ">", ">=", "<", "<=" (numeric),
"~" (substring), or "re:" (regex search).
"""
import re

KNOWN = {"tools_ordered", "tools_any_order", "tools_any_of", "forbid", "forbid_first_turn",
         "no_tools", "max_tool_calls", "args", "expect_question", "forbid_question",
         "reply_max_words", "reply_mentions_any", "reply_mentions_all", "reply_forbids"}
REQUIRED = ("id", "cat", "say", "desktop", "expect")


def validate_case(case, fixtures=(), tool_names=()):
    """Return a list of problems (empty when the case is well-formed)."""
    bad = [f"missing {k}" for k in REQUIRED if k not in case]
    if bad:
        return bad
    exp = case["expect"]
    bad += [f"unknown expect key {k}" for k in exp if k not in KNOWN]
    if fixtures and case["desktop"] not in fixtures:
        bad.append(f"unknown desktop {case['desktop']}")
    if tool_names:
        for k in ("tools_ordered", "tools_any_order", "tools_any_of", "forbid", "forbid_first_turn"):
            bad += [f"{k}: unknown tool {t}" for t in exp.get(k, []) if t not in tool_names]
        bad += [f"args: unknown tool {t}" for t in exp.get("args", {}) if t not in tool_names]
    return bad


def match_value(want, got):
    if isinstance(want, bool):
        return isinstance(got, bool) and got == want
    if isinstance(want, (int, float)):
        try:
            return abs(float(got) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    if isinstance(want, str):
        g = "" if got is None else str(got)
        for op in (">=", "<=", ">", "<"):
            if want.startswith(op):
                try:
                    a, b = float(g), float(want[len(op):])
                except ValueError:
                    return False
                return {">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        if want.startswith("~"):
            return want[1:].lower() in g.lower()
        if want.startswith("re:"):
            return re.search(want[3:], g, re.I) is not None
        return g.strip().lower() == want.strip().lower()
    return got == want


def is_subsequence(want, seq):
    it = iter(seq)
    return all(any(x == w for x in it) for w in want)


def has_question(text):
    return "?" in (text or "")


def score(case, calls, replies, first_turn_calls=None):
    """calls: [{"tool","args"}] across the whole session; replies: [str] per
    turn; first_turn_calls: names called before the first reply (defaults to
    all calls when there was only one turn). Returns {"pass", "failed": [..]}."""
    exp = case.get("expect", {})
    names = [c["tool"] for c in calls]
    first = names if first_turn_calls is None else list(first_turn_calls)
    joined = " ".join(r for r in replies if r).lower()
    last = (replies[-1] if replies else "") or ""
    failed = []

    def check(name, ok, detail=""):
        if not ok:
            failed.append(f"{name}{(': ' + detail) if detail else ''}")

    if "tools_ordered" in exp:
        check("tools_ordered", is_subsequence(exp["tools_ordered"], names), f"got {names}")
    if "tools_any_order" in exp:
        missing = [t for t in exp["tools_any_order"] if t not in names]
        check("tools_any_order", not missing, f"missing {missing}")
    if "tools_any_of" in exp:
        check("tools_any_of", any(t in names for t in exp["tools_any_of"]), f"got {names}")
    if "forbid" in exp:
        hit = [t for t in names if t in exp["forbid"]]
        check("forbid", not hit, f"called {hit}")
    if "forbid_first_turn" in exp:
        hit = [t for t in first if t in exp["forbid_first_turn"]]
        check("forbid_first_turn", not hit, f"called {hit}")
    if exp.get("no_tools"):
        check("no_tools", not names, f"called {names}")
    if "max_tool_calls" in exp:
        check("max_tool_calls", len(names) <= exp["max_tool_calls"], f"{len(names)} calls")
    for tool, want in exp.get("args", {}).items():
        cands = [c for c in calls if c["tool"] == tool]
        ok = any(all(match_value(v, c["args"].get(k)) for k, v in want.items()) for c in cands)
        check(f"args.{tool}", ok, f"want {want} got {[c['args'] for c in cands]}")
    if exp.get("expect_question"):
        check("expect_question", replies and has_question(replies[0]), f"reply={replies[:1]}")
    if exp.get("forbid_question"):
        check("forbid_question", not any(has_question(r) for r in replies))
    if "reply_max_words" in exp:
        n = len(last.split())
        check("reply_max_words", n <= exp["reply_max_words"], f"{n} words")
    if "reply_mentions_any" in exp:
        check("reply_mentions_any", any(m.lower() in joined for m in exp["reply_mentions_any"]),
              f"reply={last[:80]!r}")
    if "reply_mentions_all" in exp:
        missing = [m for m in exp["reply_mentions_all"] if m.lower() not in joined]
        check("reply_mentions_all", not missing, f"missing {missing}")
    if "reply_forbids" in exp:
        hit = [m for m in exp["reply_forbids"] if m.lower() in joined]
        check("reply_forbids", not hit, f"said {hit}")
    return {"pass": not failed, "failed": failed}


def summarize(rows):
    """Aggregate scored rows -> overall + per-category pass rates and telemetry."""
    def rate(xs):
        return round(100 * sum(1 for x in xs if x["pass"]) / len(xs)) if xs else None
    cats = sorted({r["cat"] for r in rows})
    turn_ms = [r["turn_ms"] for r in rows if r.get("turn_ms")]
    tokens = [r["usage"].get("total", 0) for r in rows if r.get("usage")]
    return {
        "cases": len(rows),
        "pass_rate": rate(rows),
        "by_cat": {c: {"n": len([r for r in rows if r["cat"] == c]),
                       "pass_rate": rate([r for r in rows if r["cat"] == c])} for c in cats},
        "forbid_violations": sum(1 for r in rows for f in r["failed"] if f.startswith("forbid")),
        "timeouts": sum(1 for r in rows if r.get("timeout")),
        "errors": sum(1 for r in rows if r.get("error")),
        "tool_failures": sum(1 for r in rows for c in r.get("calls", []) if not c.get("ok", True)),
        "avg_turn_ms": int(sum(turn_ms) / len(turn_ms)) if turn_ms else None,
        "avg_reply_words": round(sum(len((r.get("reply") or "").split()) for r in rows) / len(rows), 1) if rows else None,
        "total_tokens": sum(tokens),
    }
