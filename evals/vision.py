"""Score the vision tools (find_on_screen's locator, look_at_screen's answerer)
on fixture screenshots, using the exact prompts the shipped tools use.

    python3 evals/vision.py capture inbox          # grab the focused monitor -> evals/vision/inbox.png
    python3 evals/vision.py run                    # score every target/question
    python3 evals/vision.py run --set find --repeat 3

Fixtures live in evals/vision/:
    <name>.png            a screenshot (gitignored -- it is YOUR desktop)
    targets.json          {"<name>": [{"describe": "the Compose button",
                                        "bbox": [x0, y0, x1, y1]}]}       # pixels
    questions.json        {"<name>": [{"ask": "what colour is the button",
                                        "expect_any": ["blue"]}]}

A find hit is a returned point inside the bbox; we also record the distance in
pixels from the bbox centre. A look hit is any expect_any string in the answer.
"""
import argparse
import base64
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "beckon"))

import common   # noqa: E402
import tools    # noqa: E402

DIR = ROOT / "evals" / "vision"
RESULTS = ROOT / "evals" / "results"


def png_size(data):
    """(width, height) from the IHDR chunk -- no PIL needed."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def load_json(name):
    p = DIR / name
    return json.loads(p.read_text()) if p.exists() else {}


def capture(name):
    mon = next((m for m in tools._query("monitors") if m.get("focused")), None)
    if not mon:
        sys.exit("no focused monitor")
    DIR.mkdir(parents=True, exist_ok=True)
    out = DIR / f"{name}.png"
    r = subprocess.run(["grim", "-o", mon["name"], str(out)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(r.stderr.strip() or "grim failed")
    w, h = png_size(out.read_bytes())
    print(f"wrote {out.relative_to(ROOT)} ({w}x{h}, scale {mon.get('scale')})")
    print(f"now add targets to {DIR.relative_to(ROOT)}/targets.json under \"{name}\" with pixel bboxes")


def run_find(name, img_b64, size, targets, repeat):
    rows = []
    w, h = size
    for t in targets:
        x0, y0, x1, y1 = t["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for rep in range(repeat):
            t0 = time.monotonic()
            got = tools._locate(t["describe"], img_b64)
            ms = int((time.monotonic() - t0) * 1000)
            row = {"set": "find", "shot": name, "describe": t["describe"], "rep": rep, "ms": ms}
            if "x" in got:
                px, py = float(got["x"]) / 1000 * w, float(got["y"]) / 1000 * h
                row.update(x=int(px), y=int(py), hit=x0 <= px <= x1 and y0 <= py <= y1,
                           dist=int(math.hypot(px - cx, py - cy)))
            else:
                row.update(hit=False, error=got.get("error"), raw=got.get("raw"))
            rows.append(row)
            print(f"{'HIT ' if row['hit'] else 'MISS'} {name}: {t['describe'][:40]:<40} "
                  f"{row.get('dist', '-'):>5}px {ms:>5}ms", flush=True)
    return rows


def run_look(name, img_b64, questions, repeat):
    rows = []
    for q in questions:
        for rep in range(repeat):
            t0 = time.monotonic()
            ans = tools._ask_vision(q["ask"], img_b64, q.get("page_text"))
            ms = int((time.monotonic() - t0) * 1000)
            text = ans if isinstance(ans, str) else json.dumps(ans)
            hit = any(e.lower() in text.lower() for e in q["expect_any"])
            rows.append({"set": "look", "shot": name, "ask": q["ask"], "rep": rep, "ms": ms,
                         "hit": hit, "answer": text[:300], "words": len(text.split())})
            print(f"{'HIT ' if hit else 'MISS'} {name}: {q['ask'][:40]:<40} {ms:>5}ms  {text[:60]!r}",
                  flush=True)
    return rows


def run(opt):
    if not common.api_key():
        sys.exit("no API key")
    targets, questions = load_json("targets.json"), load_json("questions.json")
    shots = sorted(p.stem for p in DIR.glob("*.png"))
    if not shots:
        sys.exit(f"no screenshots in {DIR.relative_to(ROOT)}; run `vision.py capture <name>` first")
    rows = []
    for name in shots:
        data = (DIR / f"{name}.png").read_bytes()
        b64 = base64.b64encode(data).decode()
        if opt.set in ("find", "all") and targets.get(name):
            rows += run_find(name, b64, png_size(data), targets[name], opt.repeat)
        if opt.set in ("look", "all") and questions.get(name):
            rows += run_look(name, b64, questions[name], opt.repeat)
    if not rows:
        sys.exit("nothing to score: add entries to targets.json / questions.json")
    summary = {}
    for s in ("find", "look"):
        sub = [r for r in rows if r["set"] == s]
        if sub:
            summary[s] = {"n": len(sub), "hit_rate": round(100 * sum(r["hit"] for r in sub) / len(sub)),
                          "avg_ms": int(sum(r["ms"] for r in sub) / len(sub))}
            if s == "find":
                d = [r["dist"] for r in sub if "dist" in r]
                summary[s]["median_dist_px"] = sorted(d)[len(d) // 2] if d else None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{stamp}-vision-{common.setting('text_model')}.json"
    out.write_text(json.dumps({"meta": {"text_model": common.setting("text_model"), "stamp": stamp,
                                        "repeat": opt.repeat, "shots": shots},
                               "summary": summary, "rows": rows}, indent=1))
    print()
    for s, v in summary.items():
        print(f"{s}: {v}")
    print(f"wrote {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("name", help="fixture name; must be an identifier")
    r = sub.add_parser("run")
    r.add_argument("--set", default="all", choices=["all", "find", "look"])
    r.add_argument("--repeat", type=int, default=1)
    opt = ap.parse_args()
    if opt.cmd == "capture":
        if not re.fullmatch(r"[a-z0-9_]+", opt.name):
            sys.exit("name must be lowercase letters, digits, underscores")
        capture(opt.name)
    else:
        run(opt)


if __name__ == "__main__":
    main()
