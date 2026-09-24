# Eval baselines

Hand-curated headline numbers, one row per phase. Every later phase must beat
the row above it (or explain why not) before it merges. Raw result files are
gitignored; regenerate with the command in each row.

Phase 0–1 rows below are 79×3 full-suite runs. Later gates default to
`--suite cheap` (16×1). Do not re-run a 79×3 unless a rebaseline is asked for.

Pass rate = cases where every `expect` check held. `forbid` = calls to a tool
the case forbade (the worst kind of miss: the agent did something unasked).
`turn ms` = send → turn complete, averaged over all runs.

## Command evals (`evals/run.py`)

| phase | git | live model | thinking | input | runs | pass | forbid | tool fails | timeouts | avg turn ms | avg reply words | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 baseline | 8a2f59d | gemini-3.1-flash-live-preview | – | text | 79×3 | 89% | 10 | 22 | 0 | 3666 | 7.4 | 2.41M |
| 0 baseline | 8a2f59d | gemini-3.1-flash-live-preview | – | audio | 79×1 | 91% | 3 | 7 | 0 | 3071 | 6.9 | 0.79M |

Per-category, phase 0 (text 79×3 / audio 79×1):

| category | n text | pass text | n audio | pass audio |
|---|---|---|---|---|
| ambiguity | 15 | 93% | 5 | 100% |
| clicking | 12 | 100% | 4 | 100% |
| clipboard | 6 | 100% | 2 | 100% |
| custom | 6 | 100% | 2 | 100% |
| keybinds | 30 | 70% | 10 | 80% |
| memory | 30 | 90% | 10 | 90% |
| multi | 6 | 100% | 2 | 50% |
| reading | 27 | 93% | 9 | 100% |
| system | 18 | 100% | 6 | 100% |
| tour | 9 | 100% | 3 | 100% |
| typing | 24 | 75% | 8 | 75% |
| windows | 36 | 86% | 12 | 92% |
| workspaces | 18 | 100% | 6 | 100% |

Audio input: ASR heard 79/79 utterances faithfully (one compound split,
"fullscreen this" → "Full screen this"), so audio-mode misses below are model
misses, not hearing misses. The audio run is a single pass, so its per-case
numbers are noisier than the text run's.

Consistent misses (0/3 or 1/3) in the text baseline, for later phases to target:

- `win_close_editor` 0/3 — "close the editor" closes the *focused* window
  (Gmail) without focusing the editor first. A wrong-window destructive act.
- `hotkey_paste` 0/3 — "paste" becomes `read_clipboard` + `type_text` instead
  of `ctrl+v`. Works by accident, breaks on rich content.
- `kb_reboot_vague` 0/3 — bare "restart" tries `press_keybind("Reboot",
  force=True)` instead of asking; the forbid class we care most about.
- `mem_email_then_remember` 2/3 — once, after the user answered "gmail", the
  model launched Chrome and typed the URL rather than calling `open_url`.
- `win_focus_editor` 1/3 text, 0/1 audio — asks "which editor?" with one
  `code` window open, then calls `recall` instead of `list_windows`.
- `hotkey_undo` / `hotkey_save` — reaches for `press_keybind("Undo"/"Save")`
  (no such shortcuts) before falling back to the wrong key.
- `kb_lock_explicit` 1/3 — refuses to pass `force=True` even though the user
  asked for the lock in so many words.
- `kb_done_for_day` 1/3 — "I'm done for the day" closes every window.
- `kb_restart_waybar` 1/3 — `launch_app("killall waybar && waybar")` before
  finding the keybind.
- `mem_recall_project` 1/3 — a note that is *in the system prompt* is not
  found; the model calls `recall` and reports nothing.
- `read_invoice_amount` 1/3 — asks which window rather than reading the
  focused one.

Seen only in the audio run (single pass, so treat as leads, not trends):

- `hotkey_copy` — "copy that" becomes `read_clipboard` and reads the clipboard
  aloud instead of pressing `ctrl+c`.
- `multi_open_and_read` — `find_on_screen` then straight to `read_page_text`,
  skipping `move_mouse`/`click`; the email was never actually opened.
- `mem_recall_project` 0/1 audio too — same in-prompt-note-not-found miss.

Commands:

```
python3 evals/run.py --repeat 3 --tag baseline-text
python3 evals/run.py --input audio --tag baseline-audio
```

## Phase 1 — Gemini 3.8 Live

Text-only gate, 79×3, same harness as Phase 0. HEAD was `2bcaa0b` with the
uncommitted Phase 1 tree (`common`/`live`/`ui`/`evals`/`tests`); the `git`
column is that HEAD. Default stays `gemini-3.8-live-extended-thinking` at
`thinking_level=medium` — highest pass rate, and already `common.DEFAULTS`.

| phase | git | live model | thinking | input | runs | pass | forbid | tool fails | timeouts | avg turn ms | avg reply words | tokens |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 default | 2bcaa0b* | gemini-3.8-live-extended-thinking | medium | text | 79×3 | 97% | 5 | 32 | 0 | 8593 | 12.6 | 5.85M |
| 1 | 2bcaa0b* | gemini-3.8-live-extended-thinking | low | text | 79×3 | 96% | 2 | 7 | 0 | 8383 | 11.5 | 5.78M |
| 1 | 2bcaa0b* | gemini-3.8-live-extended-thinking | high | text | 79×3 | 94% | 7 | 53 | 1 | 11061 | 14.6 | 5.19M |
| 1 | 2bcaa0b* | gemini-3.8-live | – | text | 79×3 | 54% | 2 | 4 | 0 | 963 | 0.9 | 1.10M |

`3.8-live` is not a candidate: 107 of 109 fails had an empty reply (avg 0.9
words). It often fires one tool and closes on `turn_complete` before a spoken
answer, so `reply_mentions_*` and multi-step recipes collapse. Fast, but it
does not beat Phase 0.

High is slower and worse than medium (more forbid, more tool fails, one
timeout). Low is close on pass rate but drops `multi` (50%) and `tour` (78%);
two of its ten fails were API 1011 "service unavailable", which still leaves
it behind medium.

Per-category, Phase 0 text vs Phase 1 default (medium):

| category | n | p0 text | p1 medium |
|---|---|---|---|
| ambiguity | 15 | 93% | 100% |
| clicking | 12 | 100% | 100% |
| clipboard | 6 | 100% | 100% |
| custom | 6 | 100% | 100% |
| keybinds | 30 | 70% | 93% |
| memory | 30 | 90% | 100% |
| multi | 6 | 100% | 100% |
| reading | 27 | 93% | 89% |
| system | 18 | 100% | 100% |
| tour | 9 | 100% | 100% |
| typing | 24 | 75% | 96% |
| windows | 36 | 86% | 100% |
| workspaces | 18 | 100% | 100% |

Phase 0's consistent misses are largely gone on medium (`win_close_editor`,
`hotkey_paste`, `mem_email_then_remember`, `win_focus_editor` all 3/3).
`kb_reboot_vague` is 2/3 (still one forbid). New consistent miss:

- `look_colour` 0/3 — "what colour is the compose button" calls
  `read_page_text` (forbidden; the case wants `look_at_screen`). That is the
  whole reading dip.

Commands:

```
python3 evals/run.py --model gemini-3.8-live --repeat 3 --tag phase1-38-live
python3 evals/run.py --model gemini-3.8-live-extended-thinking --thinking medium --repeat 3 --tag phase1-38-ext-medium
python3 evals/run.py --model gemini-3.8-live-extended-thinking --thinking high --repeat 3 --tag phase1-38-ext-high
python3 evals/run.py --model gemini-3.8-live-extended-thinking --thinking low --repeat 3 --tag phase1-38-ext-low
```

## Vision evals (`evals/vision.py`)

Screenshots are captures of the maintainer's desktop and are not committed, so
these rows are only reproducible on that machine. `find` hit = predicted point
inside the target bbox; `look` hit = an expected string in the answer.

| phase | text model | find n | find hit | median px err | look n | look hit | avg ms |
|---|---|---|---|---|---|---|---|
| 0 baseline | gemini-3.8-flash | (no fixtures captured yet) | | | | | |

## Notes

- Phase 0 smoke run surfaced a real miss on the first try: "thanks, that's
  all" made `gemini-3.1-flash-live-preview` call `close_window`. That is the
  kind of thing Phases 1–4 are judged on.
