# Beckon accuracy plan

Goal: turn every available knob on the Gemini provider for **accuracy first**
(latency is a later pass), measure each change against a fixed eval set, then
add an OpenAI Realtime provider and a Codex app-server voice-mode backend
behind the same abstraction.

Ground rules for every phase:

- Every behavioural change is a `settings.json` key with the old behaviour
  reachable, so any two configurations can be A/B'd without a code edit.
- Nothing lands without an eval run. Phase 0 exists so this is cheap.
- `pytest` stays network-free and green. Network-touching evals live in
  `evals/`, not `tests/`.
- `AGENTS.md` and `README.md` are updated in the same PR as the code they
  describe. Model names stay in `common.DEFAULTS` only.
- One PR per phase, branched off `master`, merged in order. Phases 1-6 are
  Gemini-only and independent enough to be reviewed separately.

Verified as of 2026-09-17 (re-check at implementation time; the Live API is
still labelled Preview even where the model IDs are stable):

| Slot | Current | Best available | Notes |
|---|---|---|---|
| Live session | `gemini-3.1-flash-live-preview` | `gemini-3.8-live-extended-thinking` (accuracy) / `gemini-3.8-live` (fast) | Both stable, Sept 2026. Extended Thinking: `thinking_level` low/medium/high, **async tools only**, `interaction_status` lifecycle. 3.8 Live: no `thinking_level`, async tools default, BLOCKING allowed. Both: proactive audio always on, affective dialog removed. |
| Screen reads / find_on_screen | `gemini-3.8-flash` | same | GA. Default thinking `medium`; levels low/medium/high; structured outputs supported; 1M context. |
| Tour narration | `gemini-3.1-flash-tts-preview` | same | Latest TTS listed. No change. |
| OpenAI realtime | none | `gpt-realtime-2.1` | Reasoning voice model, `reasoning.effort`, 128k context, async function calling, image input, 60-min sessions. `openai` SDK not installed here. |
| Codex voice | none | `codex app-server` `thread/realtime/*` (experimental) | codex-cli 0.154.0 installed. JSON-RPC-lite over stdio. `start` takes `outputModality`, `model`, `voice`, `version` v1/v2/v3. |

Installed `google-genai` 2.23.0 already exposes everything Phases 1-6 need:
`FunctionDeclaration.behavior`, `Behavior`, `FunctionResponseScheduling`,
`ThinkingLevel`, `LiveConnectConfig.{thinking_config, session_resumption,
media_resolution, realtime_input_config, proactivity}`,
`LiveServerContent.{interaction_status, interim_input_transcription,
turn_complete_reason}`, `LiveServerMessage.{go_away, session_resumption_update,
tool_call_cancellation, usage_metadata}`.

---

## Phase 0 — Measure first: telemetry + eval harness

Nothing else can be judged without this. No model behaviour changes here.

### 0.1 Per-turn telemetry in `history.jsonl`

`live.log()` gains: `provider`, `model`, `thinking_level`, `voice`,
`usage` (from `usage_metadata`: prompt/response/total tokens, when present),
`tools: [{name, args, ms, ok}]` (replace the current `actions` shape; keep the
key name `actions` for the panel, add `ms`/`ok`), `turn_ms` (first user audio
in the turn → `turn_complete`), and `session_id` (uuid per process) so a
session's turns can be grouped. Panel history view shows `model` and tool
timings.

### 0.2 Command eval set — `evals/cases.jsonl`

~60 cases across the tool categories, each:

```json
{"id": "side_by_side", "say": "put chrome and my editor side by side",
 "desktop": "two_windows",
 "expect": {"tools_any_order": ["toggle_split"], "forbid": ["close_window"]},
 "reply_max_words": 12}
```

Fields: `tools_ordered` (in-order subsequence — unrelated calls such as
`list_windows` may sit between), `tools_any_order`, `tools_any_of`, `args`
(subset match per call; matchers `>N`, `~substring`, `re:regex`), `forbid`,
`forbid_first_turn`, `no_tools`, `max_tool_calls`, `expect_question` /
`forbid_question` (memory "ask once" cases), `reply_max_words`,
`reply_mentions_any/all`, `reply_forbids`. Optional `then: [..]` sends
follow-up utterances in the same session. `desktop` names a fixture in
`evals/fixtures/desktops/*.json` (may `base` another fixture) — canned
windows, monitors, keybinds, clipboard, page text, click targets, memory and
custom tools. Full schema in `evals/score.py`.

Categories to cover: window management, workspaces/monitors, typing/keys,
click recipe (find→move→click ordering), reading (read_page_text vs
look_at_screen mode choice), keybinds (prefer `press_keybind` over
`launch_app`), memory (use preference / ask once / never announce), clipboard,
system, custom tools, destructive-refusal (close all, lock), tour trigger vs
"what can you do", ambiguous window names (must call `list_windows` first).

### 0.3 Runner — `evals/run.py`

- `--provider gemini|openai|codex --model M --thinking L --repeat N --cases glob`
- Replaces `tools.TOOLS` with a `FakeDesktop` that answers from the fixture,
  records calls, and returns plausible results (never touches Hyprland).
- Input modes:
  - `--input text` (default): `send_client_content` (Gemini) /
    `conversation.item.create`+`response.create` (OpenAI). Deterministic.
  - `--input audio`: utterance rendered once with the existing
    `narrate.generate()` TTS path (framed as "read this aloud", since bare
    imperatives trip the TTS safety filter), read back once with `text_model`
    and regenerated if the clip doesn't say the utterance, cached in
    `~/.cache/beckon/eval/`. Streamed as real-time-paced 24 kHz PCM
    (`audio/pcm;rate=24000`; the API resamples) via `send_realtime_input`,
    then 1.5 s of silence and `audio_stream_end`. Exercises real VAD + ASR.
- Waits for turn end correctly per model: `turn_complete` on 3.1/3.8-live,
  `interaction_status == IDLE` on extended-thinking, `response.done` on
  OpenAI.
- Scores per case: tool accuracy (0/1), arg accuracy, forbidden-call
  violations, reply word count, turn ms, tokens. Writes
  `evals/results/<timestamp>-<provider>-<model>-<thinking>.json` and prints a
  table. `evals/compare.py A.json B.json` prints a diff.
- `evals/results/` is gitignored except `evals/results/BASELINES.md`, a
  hand-curated table of headline numbers per phase.

### 0.4 Vision eval set — `evals/vision/`

- Flat layout: `evals/vision/<name>.png` (captured with
  `evals/vision.py capture <name>`; gitignored — it is the maintainer's
  desktop), `targets.json` `{name: [{describe, bbox}]}` and `questions.json`
  `{name: [{ask, expect_any, page_text?}]}`. Suggested captures: Gmail inbox,
  a settings page, a terminal, the panel.
- `find`: runs `tools._locate` (the shipped prompt) on the PNG. Score:
  predicted point inside bbox; report hit rate and median pixel error.
- `look`: runs `tools._ask_vision`. Score: any `expect_any` string in the
  answer. An `--judge` flag (LLM judge via `text_model`) is deferred to
  Phase 3 when free-form answers start to matter.
- `evals/vision.py run --set find|look|all --repeat N` scores both. Model /
  thinking / resolution knobs arrive with Phase 3 alongside the settings keys
  they exercise.

### 0.5 Baseline

Run 0.3 (text + audio) and 0.4 against the current defaults
(`gemini-3.1-flash-live-preview`, `gemini-3.8-flash`, no thinking config)
three times; record in `BASELINES.md`. This is the number every later phase
must beat.

Tests: `FakeDesktop` fixture behaviour, case-file schema validation, scorer
unit tests. All offline.

---

## Phase 1 — Live model upgrade to Gemini 3.8

### Changes

- `common.DEFAULTS["model"] = "gemini-3.8-live-extended-thinking"`;
  new `DEFAULTS["thinking_level"] = "medium"` (accuracy-first default; the
  eval decides between medium and high). `"minimal"` is rejected by the API
  for this model, so `live.py` maps it to `low` with a warning.
- `live.py` builds `LiveConnectConfig` from a `MODEL_CAPS` table in
  `common.py`:

  | model | thinking_config | tool behaviour | end-of-turn signal |
  |---|---|---|---|
  | `gemini-3.8-live-extended-thinking` | required, low/medium/high | must be NON_BLOCKING | `interaction_status == IDLE` |
  | `gemini-3.8-live` | must be omitted | default NON_BLOCKING; BLOCKING allowed | `turn_complete` |
  | `gemini-3.1-flash-live-preview` | optional (`minimal` default) | sync only | `turn_complete` |

  Unknown model → treat as 3.8-live shape and log it.
- `receive()`: track `interaction_status`. Per-turn history logging moves
  from "every `turn_complete`" to "turn_complete when status is IDLE (or the
  model has no status field)". Spoken fillers ("checking that…") are appended
  to `said` but do not close the turn.
- Handle `tool_call_cancellation`: cancel/ignore in-flight results for those
  ids (matters once tools are async in Phase 2; wire the plumbing now).
- Log `usage_metadata` per turn (Phase 0 fields).
- Panel: `model` dropdown lists the three Live models with a one-line
  description; `thinking_level` select shown only when the chosen model
  supports it. `ui.py` `/api/settings` validates the pair.
- Remove nothing else yet; `enable_affective_dialog` is not set today, good.

### Tests

- Config builder: for each model in `MODEL_CAPS`, assert presence/absence of
  `thinking_config` and the `behavior` set on declarations.
- Fake-session receive test: feed a scripted sequence (filler
  `turn_complete`+`IN_PROGRESS`, tool call, final `turn_complete`+`IDLE`) and
  assert one history entry with both utterances.
- Settings validation rejects `thinking_level` for `gemini-3.8-live`.

### Eval gate

Run Phase 0 harness for `3.8-live`, `3.8-live-extended-thinking` at low /
medium / high. Pick the default from the numbers; record all rows in
`BASELINES.md`. Expect the biggest single win of the plan here.

---

## Phase 2 — Asynchronous tool calling done properly

On 3.8 models tool calls no longer pause the conversation, which changes what
`run_tools` must guarantee.

### Changes

- Each tool gets a declared `behavior` and a default result `scheduling`,
  set via a decorator in `tools.py` (`@tool(blocking=True)`,
  `@tool(scheduling="SILENT")`). Defaults for the extended-thinking model
  (everything NON_BLOCKING):

  | tools | scheduling | why |
  |---|---|---|
  | `look_at_screen`, `read_page_text`, `find_on_screen`, `recall`, `list_*`, `read_clipboard`, `current_window` | `INTERRUPT` | the answer is the point; say it as soon as it lands |
  | window/workspace/input/system actions, `set_clipboard`, `remember`, `note`, `forget`, custom tools | `SILENT` | confirmation is already spoken; result only matters if it errored → `WHEN_IDLE` on error |
  | `guided_tour`, `say` | `SILENT` | narration owns the audio |

  On `gemini-3.8-live` the ordered click recipe (`find_on_screen` →
  `move_mouse` → `click`) and `press_keybind` are declared `BLOCKING` so the
  model cannot race ahead; on extended-thinking (async only) ordering is
  enforced client-side instead: calls from one `tool_call` message run
  sequentially in one worker, and a per-session lock serialises input tools
  (`move_mouse`, `click`, `type_text`, `press_key`, `hotkey`,
  `press_keybind`) across messages.
- `receive()` spawns a task per `tool_call` message instead of awaiting
  inline, so a slow screen read no longer blocks reading the socket.
  `tool_call_cancellation` cancels the matching task; a cancelled tool's
  result is dropped, not sent.
- `FunctionResponse.response` carries `{"result": ..., "scheduling": ...}`;
  errors always use `INTERRUPT` so the model reports failures instead of
  claiming success.
- Custom shell tools default to NON_BLOCKING + `WHEN_IDLE`.

### Tests

- Declarations carry the right `behavior` per model.
- Fake-session test: two overlapping `tool_call` messages; assert input-tool
  serialisation and that a cancelled call sends no response.
- Scheduling chosen per tool and flipped to INTERRUPT on error.

### Eval gate

Click-recipe and multi-step cases (`"open the first email and read it to
me"`) must not regress vs Phase 1; expect improvement in "did it report the
error" cases.

---

## Phase 3 — Vision side channel: accuracy over speed

`look_at_screen` / `find_on_screen` are the accuracy-critical tools and today
send a full-res PNG with a one-line prompt and no thinking control.

### Changes

- `common.generate_content()` accepts `thinking_level`, `response_schema`,
  `media_resolution`; new settings `text_thinking_level` (default `high`),
  `vision_media_resolution` (default `high`). Timeout raised to 90 s; the
  glow overlay cap follows the timeout instead of a hard 45 s.
- `find_on_screen`:
  - Structured output (`responseJsonSchema` with `{x, y, confidence,
    found, reason}`) instead of regex over free text.
  - Two-pass refine: if `confidence < threshold` or on the first miss, crop
    a 40 % window around the first guess at full resolution and ask again;
    map back to monitor coords. Both passes logged with confidence.
  - Prompt names the monitor's logical size and reminds the model that
    coordinates are of the *centre* of the target.
- `look_at_screen`:
  - `_page_text()` and `_screenshot()` run concurrently
    (`ThreadPoolExecutor`), then one request.
  - Text budget raised from 20 k chars (3.8-flash has 1M context; keep a
    settings cap `page_text_max_chars`, default 200 k).
  - Keep PNG (lossless) for accuracy; add `screenshot_format` setting so the
    latency pass can try JPEG later.
- Experiment (flagged, off by default): `vision_in_session=true` sends the
  screenshot into the Live session with `send_realtime_input(media=...)`
  instead of the side channel, so the live model answers itself. Evaluate;
  probably loses to 3.8-flash+thinking but it is a real knob.

### Tests

- Structured-output parsing, crop→monitor coordinate math (with `scale`),
  concurrency helper, budget cap.

### Eval gate

`evals/vision.py` hit rate and pixel error vs Phase 0 baseline; `look/`
keyword recall. Report cost-per-call from usage metadata alongside.

---

## Phase 4 — Prompt and tool-schema accuracy

The schema builder currently emits `"description": pname` for every
parameter, so the model is guessing what `x`, `mode`, `follow`, `direction`
mean from the function docstring alone.

### Changes

- Parameter descriptions and enums: a `PARAMS` dict on each tool function
  (`fn.params = {"mode": {"description": ..., "enum": ["auto","image","text"]}}`)
  or a Google-style `Args:` section parsed from the docstring. `declarations()`
  emits `parameters_json_schema` with descriptions, enums, min/max where
  meaningful (workspace 1-10, volume 0-100).
- Tool docstrings rewritten to the pattern: one-line *when to use*, *when not
  to*, *what it returns*. Cross-references (`"prefer read_page_text"`) kept
  but deduplicated with the system prompt.
- System prompt restructured into short sections (identity, act-don't-ask,
  reading, clicking, keybinds, memory, tour). Move desktop facts
  (`eDP-1`, monitor list) into a generated "environment" block from
  `list_monitors()` at session start rather than hardcoding.
- Optional `google_search` tool in the Live config (setting
  `search_grounding`, default off) for "what's the weather" style asks that
  today become `open_url`.
- `temperature` exposed as a setting (default unset). Try 0.2–0.4 in evals for
  tool-selection consistency.

### Tests

- Declarations include descriptions/enums; no parameter is described by its
  own name; prompt builder includes the environment block.

### Eval gate

Full command set. Expect gains in the ambiguity, reading-mode-choice and
keybind-vs-launch categories.

---

## Phase 5 — VAD, transcription and mic gating (accuracy side)

Not the latency pass; this is about not cutting the user off and hearing them
correctly.

### Changes

- Settings for the whole `AutomaticActivityDetection` surface:
  `vad_start_sensitivity` (LOW today), `vad_end_sensitivity`,
  `vad_silence_ms`, `vad_prefix_padding_ms`. Defaults chosen by the audio-mode
  eval (long utterances with mid-sentence pauses must not be split).
- Consume `interim_input_transcription` for the terminal display so the
  "you:" line appears while speaking; final transcript still logged.
- `SPEAK_TAIL` becomes a setting; `BECKON_BARGE_IN` becomes `barge_in` in
  settings (env still wins). Proactive audio is always on for 3.8 models, so
  the half-duplex gate must stay correct: verify with the fake-session test
  that model audio arriving while the mic is gated never re-enters.
- Explicit push-to-talk mode (`explicit_vad_signal`) as an option for noisy
  rooms: hold the key to talk. Off by default.

### Eval gate

Audio-mode runs of the command set with utterances rendered at two speaking
paces; count truncated/duplicated turns.

---

## Phase 6 — Session robustness

Sessions currently die at the ~10-minute connection limit with no resume.

### Changes

- `session_resumption=types.SessionResumptionConfig()`; persist the handle
  from `session_resumption_update` in `STATE`; on `go_away` (arrives with
  `time_left`) or a dropped socket, reconnect with the handle and resume
  mic/speaker without user action. Notify only if resume fails.
- Keep `context_window_compression` (sliding window) and expose
  `trigger_tokens`.
- History entries record `resumed: true` on the first turn after a resume.

### Tests

- Fake-session: emit `go_away`, assert reconnect is attempted with the stored
  handle and the mic task is not duplicated.

---

## Phase 7 — Provider abstraction (prerequisite for 8 and 9)

Pure refactor; evals must be bit-for-bit unchanged before and after.

### Shape

```
beckon/
  live.py            # CLI, PID file, mic/speaker, half-duplex gate, history
  providers/
    base.py          # Provider, Session, Event dataclasses
    gemini.py        # everything that imports google.genai
    openai_rt.py     # Phase 8
    codex.py         # Phase 9
```

`Session` interface: `send_audio(pcm16, rate)`, `send_text(str)`,
`send_tool_results([(call_id, name, result, scheduling)])`, `events()` →
async iterator of `Audio(bytes, rate)`, `InputTranscript(text, final)`,
`OutputTranscript(text)`, `ToolCall(id, name, args)`, `ToolCancel(ids)`,
`Interrupted`, `TurnComplete(final: bool)`, `GoAway(seconds)`, `Usage(dict)`,
`Error(str)`. `Provider.declare_tools(TOOLS)` converts the neutral tool
schema (Phase 4) to the provider's format; `Provider.audio_rates()` returns
(in, out) so `live.py` opens sounddevice streams at the right rates
(Gemini 16k/24k, OpenAI 24k/24k).

Settings: `provider` (`gemini` default). Key lookup becomes per-provider
(`api_key` stays the Gemini file; `openai_api_key` added). Panel gets a
provider selector that swaps the model/voice lists.

### Tests

Existing live tests move to `tests/test_gemini_provider.py`; add
`tests/test_provider_contract.py` that runs a scripted conversation through
a `FakeProvider` and asserts `live.py` behaviour (gating, logging, tool
dispatch) is provider-independent.

---

## Phase 8 — OpenAI Realtime provider (`gpt-realtime-2.1`)

### Changes

- `providers/openai_rt.py` over raw `websockets` (already a dependency; avoid
  adding the `openai` SDK unless the GA event shapes prove awkward).
  `wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1`, `Authorization:
  Bearer`, `session.update` with `type: "realtime"`, `output_modalities:
  ["audio"]`, `audio.input/output` format pcm16 (verify exact field names
  against the GA reference when implementing), `audio.input.transcription`
  enabled, `turn_detection` server VAD (or `semantic_vad`), `tools` in JSON
  Schema, `tool_choice: auto`, `reasoning.effort` from a new
  `openai_reasoning_effort` setting (default `medium` for accuracy; docs
  recommend `low` for latency).
- Event mapping: `response.output_audio.delta` → `Audio`;
  `response.output_audio_transcript.delta` → `OutputTranscript`;
  `conversation.item.input_audio_transcription.completed` →
  `InputTranscript(final)`; `response.done` items of type `function_call` →
  `ToolCall`; `input_audio_buffer.speech_started` while playing →
  `Interrupted` (+ `response.cancel`, `conversation.item.truncate`);
  `response.done` → `TurnComplete`.
- Tool results: `conversation.item.create {type: function_call_output}` then
  `response.create`. Async function calling is native on GA models; no
  scheduling concept, so `scheduling` is ignored here.
- Image input is supported: `look_at_screen` can optionally attach the
  screenshot as a user image item in-session (mirror of the Phase 3
  experiment). Side channel remains `text_model` (Gemini) by default; add
  `openai_text_model` if a full-OpenAI configuration is wanted.
- Voices: `marin`, `cedar`, plus the older set; list in `ui.py` per provider.
- 60-minute max session; no resume handle in the API, so Phase 6's reconnect
  path starts a fresh session and re-injects the memory block.

### Tests

Fake WebSocket server (`websockets.serve` in-process) replaying recorded GA
event JSON; assert the event mapping and the tool round-trip.

### Eval gate

Same command set, text and audio modes, `reasoning.effort` low/medium/high.
Publish side-by-side with the best Gemini configuration in `BASELINES.md`.

---

## Phase 9 — Codex app-server voice mode

Different architecture: Codex owns the Realtime connection, the agent loop and
the tools; Beckon becomes (a) the audio client and (b) an MCP server that
exposes the desktop tools to Codex. Upside: ChatGPT sign-in instead of an API
key, and Codex's background-agent handoff for long tasks. Downside: the API is
explicitly experimental and moves week to week (0.154.0 installed here; pin
and re-verify).

### Changes

- `providers/codex.py`: spawn `codex app-server` (stdio JSONL, JSON-RPC-lite),
  `initialize` with the experimental-API capability, `thread/start`, then
  `thread/realtime/start {threadId, outputModality: "audio", model, voice,
  version}` (default `version: "v2"` — Realtime Voice API path; make `v3`
  selectable). Stream mic with `thread/realtime/appendAudio`; map
  `thread/realtime/*` audio-frame and transcript notifications to `Audio` /
  `InputTranscript` / `OutputTranscript`; `thread/realtime/stop` on exit.
  Audio frame format (rate, encoding) is taken from the notification schema
  at implementation time.
- `beckon/mcp_server.py`: stdio MCP server over `tools.TOOLS` using the
  neutral schema from Phase 4. Prefer hand-rolled JSON-RPC (the surface is
  `initialize`, `tools/list`, `tools/call`) to avoid a new dependency;
  switch to the `mcp` package only if the protocol version negotiation gets
  in the way. `install.sh` offers to add
  `[mcp_servers.beckon] command = "python3" args = [".../mcp_server.py"]`
  to `~/.codex/config.toml`.
- Codex's own tools (shell, file edits, approvals) become reachable by voice.
  Approval requests arrive as server→client JSON-RPC requests; Beckon answers
  them via a spoken prompt + `press_keybind`-free yes/no, or auto-declines
  anything destructive. This needs a design decision before implementation
  (see Open questions).
- `history.jsonl` records `provider: codex`, thread id, and handoff events.

### Tests

Fake app-server process (Python script speaking the same JSONL) for the
handshake and notification mapping; MCP server tested with a scripted
`tools/list` + `tools/call`.

### Eval gate

The command set runs through `thread/realtime/appendText` for the text mode.
Expect different failure modes (Codex may delegate desktop tasks to the
background agent); score tool calls as observed at the MCP server.

---

## Phase 10 — Latency pass (deferred by design)

Listed so nothing is forgotten; run only after Phases 1-6 settle:
`gemini-3.8-live` vs extended-thinking `low`; JPEG/downscaled screenshots;
`text_thinking_level=low` for `find_on_screen` only; VAD `silence_ms`;
`SPEAK_TAIL`; dedicated output thread instead of `to_thread` per chunk;
`CHUNK` size; dropping input transcription. Same harness, `turn_ms` column.

---

## Order and dependencies

```
0 ─► 1 ─► 2 ─► 3 ─► 4 ─► 5 ─► 6 ─► 7 ─► 8
                                    └──► 9
                                          └► 10
```

3 and 4 can be developed in parallel after 2; 8 and 9 in parallel after 7.

## Open questions to settle before the relevant phase

1. **Extended-thinking as the default (Phase 1).** It is the accuracy pick,
   but every tool must be async and the client tracks `interaction_status`.
   If evals show `gemini-3.8-live` within noise, the simpler model wins.
2. **Structured-output availability on 3.8-flash for image prompts
   (Phase 3).** Documented as supported; confirm with one call before
   building the two-pass refine on it.
3. **Codex approvals by voice (Phase 9).** Auto-decline everything, or
   speak the request and accept a verbal yes? Verbal yes is the useful
   version and the risky one.
4. **Codex realtime version and model (Phase 9).** Which of v1/v2/v3 exposes
   MCP tool calls to the realtime model directly vs via background-agent
   handoff, and whether `model: gpt-realtime-2.1` is honoured. Check against
   the installed binary's `app-server-protocol` schema, not the docs.
