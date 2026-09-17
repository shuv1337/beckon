"""Diff two eval result files: per-category pass rates and every case whose
outcome changed.

    python3 evals/compare.py evals/results/A.json evals/results/B.json
"""
import json
import sys
from collections import defaultdict


def load(path):
    d = json.loads(open(path).read())
    by_id = defaultdict(list)
    for r in d["rows"]:
        by_id[r["id"]].append(r)
    return d, by_id


def rate(rows):
    return round(100 * sum(1 for r in rows if r["pass"]) / len(rows)) if rows else None


def main(a_path, b_path):
    a, a_rows = load(a_path)
    b, b_rows = load(b_path)
    print(f"A: {a['meta']['model']} {a['meta']['input']} thinking={a['meta'].get('thinking')}  "
          f"({a['summary']['cases']} runs, git {a['meta'].get('git')})")
    print(f"B: {b['meta']['model']} {b['meta']['input']} thinking={b['meta'].get('thinking')}  "
          f"({b['summary']['cases']} runs, git {b['meta'].get('git')})\n")

    sa, sb = a["summary"], b["summary"]
    print(f"{'':<14}{'A':>6}{'B':>6}{'delta':>8}")
    print(f"{'overall':<14}{sa['pass_rate']:>5}%{sb['pass_rate']:>5}%{sb['pass_rate'] - sa['pass_rate']:>+7}")
    for cat in sorted(set(sa["by_cat"]) | set(sb["by_cat"])):
        ra = (sa["by_cat"].get(cat) or {}).get("pass_rate")
        rb = (sb["by_cat"].get(cat) or {}).get("pass_rate")
        d = f"{rb - ra:+}" if ra is not None and rb is not None else "-"
        print(f"{cat:<14}{str(ra) + '%' if ra is not None else '-':>6}"
              f"{str(rb) + '%' if rb is not None else '-':>6}{d:>8}")
    for k in ("avg_turn_ms", "avg_reply_words", "forbid_violations", "tool_failures", "timeouts",
              "errors", "total_tokens"):
        print(f"{k:<18}{sa.get(k)!s:>10}{sb.get(k)!s:>10}")

    print("\nchanged cases:")
    changed = 0
    for cid in sorted(set(a_rows) | set(b_rows)):
        ra, rb = rate(a_rows.get(cid, [])), rate(b_rows.get(cid, []))
        if ra == rb:
            continue
        changed += 1
        fails = "; ".join(sorted({f for r in b_rows.get(cid, []) for f in r["failed"]}))[:90]
        print(f"  {cid:<28} {str(ra) + '%' if ra is not None else '-':>5} -> "
              f"{str(rb) + '%' if rb is not None else '-':>5}  {fails}")
    if not changed:
        print("  (none)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
