# Eval baselines

Hand-curated headline numbers, one row per phase. Every later phase must beat
the row above it (or explain why not) before it merges. Raw result files are
gitignored; regenerate with the command in each row.

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
