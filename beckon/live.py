#!/usr/bin/env python3
"""Beckon Live -- realtime voice control for Omarchy via the Gemini Live API.

A persistent WebSocket session: audio streams up continuously, Gemini streams
audio back, and tool calls fire mid-sentence. No record/transcribe/respond
round trip, so it answers while you are still talking.

  beckon live          start a session (Ctrl+C or F8 again to stop)
  beckon live --once   same, but exits after the first reply (for testing)
"""

import asyncio
import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
try:   # F8 starts us without a TTY; don't lose crash lines to block buffering
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass
import common  # noqa: E402
import tools   # noqa: E402
import memory  # noqa: E402

import sounddevice as sd  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

HISTORY = common.DATA / "history.jsonl"
STATE = common.STATE
STATE.mkdir(parents=True, exist_ok=True)
PIDFILE = STATE / "live.pid"
MUTE = STATE / "mute"   # present while the tour narrates

# Input tools share a per-session lock so overlapping NON_BLOCKING messages
# cannot race click-recipe order (find → move → click) on extended-thinking.
INPUT_TOOLS = frozenset({
    "move_mouse", "click", "type_text", "press_key", "hotkey", "press_keybind",
})

# Environment wins (the panel's start button sets it), then settings.json, so
# the choices made in the panel also apply when the keybind starts a session.
MODEL = os.environ.get("BECKON_LIVE_MODEL") or common.setting("model")
VOICE = os.environ.get("BECKON_LIVE_VOICE") or common.setting("voice")
# Blank env/setting is not "unset": required models still get a level via
# resolved_thinking. Read settings.json directly -- setting() overlays DEFAULTS
# and would always yield "medium". The panel sets BECKON_LIVE_THINKING on start.
_THINKING_ENV = os.environ.get("BECKON_LIVE_THINKING")
_THINKING_RAW = (_THINKING_ENV if _THINKING_ENV is not None
                 else common.raw_setting("thinking_level"))
THINKING = common.resolved_thinking(MODEL, _THINKING_RAW)
_UNKNOWN_WARNED = set()
# By default the mic is gated while the model speaks, so its own voice coming
# back through the speakers can't be mistaken for you interrupting it. Set
# BECKON_BARGE_IN=1 (headphones) to keep the mic open and allow talking over it.
BARGE_IN = os.environ.get("BECKON_BARGE_IN") == "1"
SPEAK_TAIL = 0.4   # seconds to keep the mic closed after playback drains

IN_RATE, OUT_RATE, CHUNK = 16000, 24000, 1024

# Every history entry carries these, so turns can be grouped by session and
# compared across models when settings change between runs.
PROVIDER = "gemini"
SESSION_ID = uuid.uuid4().hex[:12]

SYSTEM = """You are Beckon, a desktop agent with real tools that open apps and sites,
move windows, type, and click. You are NOT "just a voice model": when the user
asks you to do something on screen, do it with the tools.

You control a Linux desktop running Omarchy (Hyprland, Wayland).

You are in a live spoken conversation. Act on what the user asks -- do not read
back plans or ask permission for ordinary window management.

MEMORY
- For anything generic -- "open my email", "play some music", "open my notes",
  "my editor" -- use the saved preference from memory. If there is none, ask
  ONE short question ("Gmail, or something else?"), then call remember() with
  the answer so you never ask again, then do it.
- When the user tells you what they are working on, or says "remember this",
  call note() or remember(). Do not announce that you saved it; just carry on.
- Never store secrets, passwords, or full email addresses.

- Call list_windows or current_window first when they name a window, so you act
  on the right one.
- Keep spoken replies to a short sentence. Often a two-word confirmation is
  plenty; if the action is obvious, say almost nothing.
- Never call close_window unless they clearly asked to close something. It will
  refuse to close Claude Desktop or the Beckon panel; when asked to close
  everything, close the rest and mention those two are still open.
- To CLICK something -- "open the first email", "press that button", "select
  that" -- the recipe is always: find_on_screen("<what it is>") to get x,y,
  then move_mouse(x, y), then click(). Never say you can't click; use these.
READING WHAT IS ON SCREEN -- three ways, pick deliberately:
- read_page_text() returns the FULL text of the window, including everything
  scrolled off screen. Reach for this FIRST for an email, an article, a
  document, a chat log: it is instant, exact, and needs no scrolling.
- look_at_screen(question, mode="image") sends a screenshot. Use it for
  anything visual -- layout, colours, a photo, a video, a terminal -- or when
  read_page_text says the window publishes no text.
- look_at_screen(question) sends BOTH the screenshot and the full text. Use it
  when the answer needs how the page looks as well as what it says.
Never ask the user to scroll so you can see more -- read the text instead.
- Anything the user could do with a keyboard shortcut -- terminal, browser, file
  manager, screenshot, emoji picker, clipboard history, workspace switching --
  do with press_keybind(name); use list_keybinds to find the name. These are the
  user's CURRENT bindings, so prefer them over guessing program names.
- "Side by side" is usually toggle_split. "Put it on the TV" is
  move_workspace_to_monitor.
- The laptop screen is eDP-1; other monitors are external.

GUIDED TOUR
Call guided_tour() ONLY if they clearly asked for a tour, a demo, or to
"show me around". "What can you do" / "help" / "what are you" is a short
spoken answer listing capabilities -- do NOT start the tour. If you do
call it, call it ONCE, post_to_x=false unless they asked to post, then
say nothing until they speak again. The tour narrates itself.
"""

PY_TO_JSON = {int: "INTEGER", float: "NUMBER", bool: "BOOLEAN", str: "STRING"}
FALSY = {"false", "0", "no", "off", ""}

notify = common.notify
api_key = common.api_key


def param_type(p):
    """The Python type a parameter's default implies. bool is checked before
    int because bool is a subclass of int -- getting this wrong once declared
    every flag as a STRING, and bool("false") is True."""
    d = p.default
    if isinstance(d, bool):
        return bool
    if isinstance(d, int):
        return int
    if isinstance(d, float):
        return float
    return str


def declarations(registry=None):
    """Build Gemini function declarations from a tool registry (tools.TOOLS by
    default; the eval harness passes a fake desktop's registry)."""
    out = []
    for name, fn in (registry if registry is not None else tools.TOOLS).items():
        props, required = {}, []
        for pname, p in inspect.signature(fn).parameters.items():
            props[pname] = {"type": PY_TO_JSON[param_type(p)], "description": pname}
            if p.default is inspect.Parameter.empty:
                required.append(pname)
        d = {"name": name, "description": (fn.__doc__ or name).strip()}
        if props:
            d["parameters"] = {"type": "OBJECT", "properties": props, "required": required}
        out.append(d)
    return out


def coerce_args(fn, args):
    """Cast the model's arguments to the types the signature implies. The
    schema already asks for the right types; this is the second lock, so a
    string "false" for force= can never read as True."""
    out = dict(args or {})
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return out
    for k, v in out.items():
        p = params.get(k)
        if p is None or p.default is inspect.Parameter.empty:
            continue
        kind = param_type(p)
        try:
            if kind is bool and not isinstance(v, bool):
                out[k] = str(v).strip().lower() not in FALSY
            elif kind is int and not isinstance(v, bool):
                out[k] = int(float(v))
            elif kind is float:
                out[k] = float(v)
        except (TypeError, ValueError):
            pass   # leave it; the tool reports its own error
    return out


def run_tools(calls, registry=None, input_lock=None, skip_ids=None):
    """Execute one tool_call message's functions in order, off the event loop.
    Order matters (find_on_screen -> move_mouse -> click), so they run
    sequentially in a single worker thread. skip_ids are cancelled before
    start and yield None. Input tools take input_lock so overlapping
    messages cannot interleave a click recipe."""
    reg = registry if registry is not None else tools.TOOLS
    skip_ids = skip_ids or set()
    results = []
    for call in calls:
        cid = getattr(call, "id", None)
        if cid in skip_ids:
            results.append(None)
            continue
        fn = reg.get(call.name)
        lock = input_lock if (input_lock is not None and call.name in INPUT_TOOLS) else None
        t0 = time.monotonic()
        try:
            if lock:
                lock.acquire()
            try:
                if cid in skip_ids:
                    results.append(None)
                    continue
                result = fn(**coerce_args(fn, call.args)) if fn else {"error": "unknown tool"}
            finally:
                if lock:
                    lock.release()
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        results.append({"result": result, "ms": int((time.monotonic() - t0) * 1000),
                        "ok": tool_ok(result), "fn": fn})
    return results


def tool_ok(result):
    """A tool 'failed' if it reported an error or refused; anything else counts."""
    return not (isinstance(result, dict) and ("error" in result or "refused" in result))


def scheduling_for(fn, ok):
    """FunctionResponse scheduling. Errors always INTERRUPT so the model reports them."""
    if not ok:
        return "INTERRUPT"
    raw = getattr(fn, "_beckon_scheduling", None) or "SILENT"
    if raw not in ("SILENT", "INTERRUPT", "WHEN_IDLE"):
        raw = "SILENT"
    return raw


def tool_response(call, row, fn=None):
    """One FunctionResponse. Scheduling lives in the payload — the SDK field
    is rejected by current Live models ('not supported for this model')."""
    fn = fn if fn is not None else row.get("fn")
    sched = scheduling_for(fn, row["ok"])
    return types.FunctionResponse(
        id=getattr(call, "id", None), name=call.name,
        response={"result": row["result"], "scheduling": sched})


def stamp_behavior(decls, model, non_blocking=None, registry=None, async_tools=True):
    """Copy declarations and stamp Behavior per MODEL_CAPS + @tool(blocking).

    non_blocking=None follows caps (and per-tool BLOCKING on 'either');
    True forces NON_BLOCKING on every decl; False leaves them unstamped.
    async_tools=False restores the Phase 1 stamp (blanket NON_BLOCKING on
    extended-thinking only).
    """
    caps = common.model_caps(model)
    if not async_tools:
        if non_blocking is False or (non_blocking is None and caps["tools"] != "non_blocking"):
            return [dict(d) for d in decls]
        return [dict(d, behavior="NON_BLOCKING") for d in decls]
    if non_blocking is False or caps["tools"] == "sync":
        return [dict(d) for d in decls]
    if non_blocking is True or caps["tools"] == "non_blocking":
        return [dict(d, behavior="NON_BLOCKING") for d in decls]
    # 'either': default NON_BLOCKING; BLOCKING allowed for @tool(blocking=True)
    reg = registry if registry is not None else tools.TOOLS
    out = []
    for d in decls:
        fn = reg.get(d["name"]) if isinstance(d, dict) else None
        blocking = bool(getattr(fn, "_beckon_blocking", False))
        out.append(dict(d, behavior="BLOCKING" if blocking else "NON_BLOCKING"))
    return out


def live_config(voice=None, known=None, tool_decls=None, model=None,
                thinking=None, non_blocking=None, registry=None, async_tools=True):
    """The LiveConnectConfig a real session uses. The eval harness calls this
    too, so what gets measured is exactly what gets shipped: same prompt, same
    tool schema, same VAD and transcription settings.

    voice/known/tool_decls default to the live session's values; the harness
    passes known="" so a run is not coloured by whatever is in memory.json.

    thinking=None means "use the model's default" via resolved_thinking, so
    evals do not inherit the user's settings.json. Live.run() passes
    thinking=THINKING. Stamp behavior here, not on declarations() — per-tool
    BLOCKING on 3.8-live, all NON_BLOCKING on extended-thinking.
    """
    model = model or MODEL
    caps = common.model_caps(model)
    if model not in common.MODEL_CAPS and model not in _UNKNOWN_WARNED:
        print(f"unknown live model {model!r}; treating as gemini-3.8-live",
              file=sys.stderr)
        _UNKNOWN_WARNED.add(model)
    try:   # be less trigger-happy about faint bleed-through counting as speech
        vad = types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW))
    except AttributeError:
        vad = None
    if known is None:
        known = memory.render()
    decls = stamp_behavior(
        list(tool_decls if tool_decls is not None else declarations()),
        model, non_blocking=non_blocking, registry=registry, async_tools=async_tools)
    kwargs = dict(
        realtime_input_config=vad,
        response_modalities=["AUDIO"],
        system_instruction=SYSTEM + ("\n\n" + known if known else ""),
        tools=[{"function_declarations": decls}],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice or VOICE))),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        # let the API manage context instead of us trimming by hand
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()),
    )
    if thinking is not None and common.thinking_was_mapped(model, thinking):
        print(f"thinking_level 'minimal' is not accepted by {model}; using 'low'",
              file=sys.stderr)
    level = common.resolved_thinking(model, thinking)
    if level:
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=getattr(types.ThinkingLevel, level.upper()))
    return types.LiveConnectConfig(**kwargs)


def turn_is_complete(sc):
    """True when this server_content closes the turn.

    Models that emit interaction_status close on IDLE only -- a filler
    turn_complete with IN_PROGRESS ("checking that…") is appended to said
    but does not finish the turn. Models without the field close on
    turn_complete, matching the 3.1 / 3.8-live shape.
    """
    if sc is None:
        return False
    status = getattr(sc, "interaction_status", None)
    if status is not None:
        name = str(getattr(status, "name", status))
        return name.endswith("IDLE") and "UNSPECIFIED" not in name
    return bool(getattr(sc, "turn_complete", None))


def usage_dict(um):
    """Flatten usage_metadata to plain ints; None fields are dropped."""
    out = {}
    for k in ("prompt_token_count", "response_token_count", "total_token_count",
              "thoughts_token_count", "tool_use_prompt_token_count"):
        v = getattr(um, k, None)
        if isinstance(v, int):
            out[k.removesuffix("_token_count")] = v
    return out


def log(entry):
    """Append one turn to the local history file.

    The history is a transcript of everything said in this room, so it is
    created 0600 and never leaves the machine. Nothing here is ever uploaded
    or committed -- .gitignore covers it, and the panel can clear it.
    """
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    entry["via"] = "live"
    entry["session"] = SESSION_ID
    entry["provider"] = PROVIDER
    entry["model"] = MODEL
    entry["voice"] = VOICE
    if THINKING is not None:
        entry["thinking_level"] = THINKING
    fd = os.open(HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry) + "\n")


class Live:
    def __init__(self, once=False, async_tools=None, registry=None):
        self.once = once
        # None → Phase 2 on (evals/tests do not inherit settings.json).
        # Live.run() passes the user's setting.
        self.async_tools = True if async_tools is None else bool(async_tools)
        self.registry = registry
        self.stop = asyncio.Event()
        self.out_q = asyncio.Queue()
        self.said, self.heard, self.actions = [], [], []
        self.cancelled = set()     # tool_call_cancellation ids this turn
        self.tool_tasks = {}       # call id → Task
        self._running = set()      # in-flight run_tool_message tasks
        self.input_lock = threading.Lock()
        self.last_played = 0.0
        self.turn_started = None   # first sign of the user's turn, for turn_ms
        self.usage = None          # last usage_metadata seen this turn

    def model_speaking(self):
        """True while model audio is queued/playing (plus a short tail)."""
        if BARGE_IN:
            return False
        return (not self.out_q.empty()) or (time.monotonic() - self.last_played < SPEAK_TAIL)

    async def mic(self, session):
        """Stream microphone audio up to Gemini continuously."""
        loop = asyncio.get_running_loop()
        q = asyncio.Queue()

        def cb(indata, frames, time_info, status):
            loop.call_soon_threadsafe(q.put_nowait, bytes(indata))

        with sd.RawInputStream(samplerate=IN_RATE, blocksize=CHUNK, channels=1,
                               dtype="int16", callback=cb):
            while not self.stop.is_set():
                data = await q.get()
                if MUTE.exists() or self.model_speaking():
                    continue  # don't feed the speakers back into the model
                await session.send_realtime_input(
                    audio=types.Blob(data=data, mime_type=f"audio/pcm;rate={IN_RATE}")
                )

    async def speaker(self):
        """Play audio chunks as they arrive -- this is what makes it feel instant."""
        with sd.RawOutputStream(samplerate=OUT_RATE, channels=1, dtype="int16") as out:
            while not self.stop.is_set():
                chunk = await self.out_q.get()
                if chunk is None or MUTE.exists():
                    continue   # never let the model talk over the tour
                await asyncio.to_thread(out.write, chunk)
                self.last_played = time.monotonic()

    async def receive(self, session):
        """Handle everything coming back: audio, transcripts, tool calls."""
        while not self.stop.is_set():
            async for msg in session.receive():
                await self.handle_message(session, msg)

    async def handle_message(self, session, msg):
        """One LiveServerMessage. Extracted so tests can drive receive
        without a real session.receive() loop."""
        sc = getattr(msg, "server_content", None)
        if self.turn_started is None and (sc or getattr(msg, "tool_call", None)):
            self.turn_started = time.monotonic()
        um = getattr(msg, "usage_metadata", None)
        if um:
            self.usage = usage_dict(um)

        if getattr(msg, "data", None) and not MUTE.exists():
            self.last_played = time.monotonic()   # close the mic the moment audio arrives
            self.out_q.put_nowait(msg.data)   # dropped while the tour narrates

        cancel = getattr(msg, "tool_call_cancellation", None)
        if cancel and getattr(cancel, "ids", None):
            self.cancelled.update(cancel.ids)
            if self.async_tools:
                seen = set()
                for cid in cancel.ids:
                    t = self.tool_tasks.get(cid)
                    if t is not None and t not in seen and not t.done():
                        t.cancel()
                        seen.add(t)

        if sc:
            it = getattr(sc, "input_transcription", None)
            if it and getattr(it, "text", None):
                self.heard.append(it.text)
            ot = getattr(sc, "output_transcription", None)
            if ot and getattr(ot, "text", None):
                self.said.append(ot.text)

            if getattr(sc, "interrupted", None):
                # user talked over the model -- drop queued audio
                while not self.out_q.empty():
                    self.out_q.get_nowait()

        # Tools before turn-complete so an IDLE in the same message still
        # records the call, and so cancellation can land while a slow tool runs.
        tc = getattr(msg, "tool_call", None)
        if tc and getattr(tc, "function_calls", None):
            calls = list(tc.function_calls)
            task = asyncio.create_task(self.run_tool_message(session, calls))
            self._running.add(task)
            task.add_done_callback(self._running.discard)
            for call in calls:
                cid = getattr(call, "id", None)
                if cid is not None:
                    self.tool_tasks[cid] = task
            if not self.async_tools:
                await task

        if sc and turn_is_complete(sc):
            await self.drain_tools()
            self.finish_turn()
            if self.once:
                self.stop.set()

    async def drain_tools(self):
        """Wait for in-flight tool tasks. Used at turn end so history is complete."""
        running = [t for t in self._running if not t.done()]
        if running:
            await asyncio.gather(*running, return_exceptions=True)

    async def run_tool_message(self, session, calls):
        """One tool_call message: sequential worker, then send uncancelled results."""
        if self.async_tools:
            pending = [c for c in calls if getattr(c, "id", None) not in self.cancelled]
        else:
            pending = list(calls)
        if not pending:
            return
        try:
            results = await asyncio.to_thread(
                run_tools, pending, self.registry, self.input_lock,
                self.cancelled if self.async_tools else None)
        except asyncio.CancelledError:
            return
        responses = []
        for call, r in zip(pending, results):
            if r is None:
                continue
            self.actions.append({"tool": call.name, "args": dict(call.args or {}),
                                 "ms": r["ms"], "ok": r["ok"]})
            if getattr(call, "id", None) in self.cancelled:
                continue
            responses.append(tool_response(call, r))
        if responses:
            await session.send_tool_response(function_responses=responses)

    def finish_turn(self):
        """Log one completed turn -- transcripts, tool calls with timings, token
        usage, wall time -- then reset for the next one."""
        heard = "".join(self.heard).strip()
        said = "".join(self.said).strip()
        if heard or said or self.actions:
            entry = {"heard": heard, "reply": said, "actions": self.actions}
            if self.turn_started is not None:
                entry["turn_ms"] = int((time.monotonic() - self.turn_started) * 1000)
            if self.usage:
                entry["usage"] = self.usage
            log(entry)
            ran = "".join(f"\n  ran: {a['tool']} {a['ms']}ms{'' if a['ok'] else ' FAILED'}"
                          for a in self.actions)
            print(f"  you: {heard}\n  beckon: {said}{ran}")
        self.heard, self.said, self.actions = [], [], []
        self.cancelled = set()
        self.tool_tasks = {}
        self.turn_started, self.usage = None, None

    async def run(self):
        key = api_key()
        if not key:
            notify("No API key set — open the Beckon panel", "critical")
            print("no API key", file=sys.stderr)
            return 1

        client = genai.Client(api_key=key)
        # THINKING is already resolved; warn here so a settings.json "minimal"
        # still surfaces (live_config only warns on the raw value it is given).
        if common.thinking_was_mapped(MODEL, _THINKING_RAW):
            print(f"thinking_level 'minimal' is not accepted by {MODEL}; using 'low'",
                  file=sys.stderr)
        config = live_config(thinking=THINKING, async_tools=self.async_tools)

        PIDFILE.write_text(str(os.getpid()))
        MUTE.unlink(missing_ok=True)   # a cancelled tour must not leave us deaf
        notify("Listening — talk to me", "low")
        think = f", thinking {THINKING}" if THINKING else ""
        print(f"Beckon live [{MODEL}{think}, voice {VOICE}] — Ctrl+C to stop\n")
        indicator = None if self.once else start_listen_indicator()

        status = 0
        try:
            async with client.aio.live.connect(model=MODEL, config=config) as session:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self.mic(session))
                    tg.create_task(self.speaker())
                    tg.create_task(self.receive(session))
                    await self.stop.wait()
                    raise asyncio.CancelledError
        except* asyncio.CancelledError:
            pass
        except* Exception as eg:
            msg = "; ".join(str(e)[:120] for e in eg.exceptions[:2])
            notify(f"Live error: {msg}", "critical")
            print("error:", msg, file=sys.stderr)
            status = 1
        finally:
            stop_listen_indicator(indicator)
            PIDFILE.unlink(missing_ok=True)
            MUTE.unlink(missing_ok=True)
            notify("Session ended", "low")
        return status


def start_listen_indicator():
    """Click-through pulsing dot while the session is up. None if disabled
    or Quickshell isn't installed. live.py owns the process lifetime."""
    import shutil
    if not common.setting_on("listen_indicator"):
        return None
    qml = HERE / "listen.qml"
    if not shutil.which("quickshell") or not qml.exists():
        return None
    return subprocess.Popen(["quickshell", "-p", str(qml)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_listen_indicator(proc):
    if proc is None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except Exception:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except Exception:
            pass


def is_live_pid(pid):
    """True only if pid is a running Beckon live session -- not whatever
    process happens to have been handed a stale PID since."""
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"live.py" in cmdline


def main():
    once = "--once" in sys.argv

    # F8 pressed while a session runs -> stop it
    if PIDFILE.exists() and "--force" not in sys.argv:
        try:
            pid = int(PIDFILE.read_text().strip())
            if is_live_pid(pid):
                os.kill(pid, signal.SIGTERM)
                PIDFILE.unlink(missing_ok=True)
                notify("Session ended", "low")
                return 0
            PIDFILE.unlink(missing_ok=True)   # stale: the PID belongs to something else now
        except (ValueError, ProcessLookupError):
            PIDFILE.unlink(missing_ok=True)

    live = Live(once=once, async_tools=common.setting_on("async_tools"))

    def bye(*_):
        live.stop.set()
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    return asyncio.run(live.run())


if __name__ == "__main__":
    sys.exit(main())
