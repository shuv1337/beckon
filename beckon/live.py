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
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
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

# Environment wins (the panel's start button sets it), then settings.json, so
# the choices made in the panel also apply when the keybind starts a session.
MODEL = os.environ.get("BECKON_LIVE_MODEL") or common.setting("model")
VOICE = os.environ.get("BECKON_LIVE_VOICE") or common.setting("voice")
# By default the mic is gated while the model speaks, so its own voice coming
# back through the speakers can't be mistaken for you interrupting it. Set
# BECKON_BARGE_IN=1 (headphones) to keep the mic open and allow talking over it.
BARGE_IN = os.environ.get("BECKON_BARGE_IN") == "1"
SPEAK_TAIL = 0.4   # seconds to keep the mic closed after playback drains

IN_RATE, OUT_RATE, CHUNK = 16000, 24000, 1024

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


def declarations():
    """Build Gemini function declarations from tools.TOOLS."""
    out = []
    for name, fn in tools.TOOLS.items():
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


def run_tools(calls):
    """Execute one tool_call message's functions in order, off the event loop.
    Order matters (find_on_screen -> move_mouse -> click), so they run
    sequentially in a single worker thread."""
    results = []
    for call in calls:
        fn = tools.TOOLS.get(call.name)
        try:
            result = fn(**coerce_args(fn, call.args)) if fn else {"error": "unknown tool"}
        except Exception as e:
            result = {"error": f"{type(e).__name__}: {e}"}
        results.append(result)
    return results


def log(entry):
    """Append one turn to the local history file.

    The history is a transcript of everything said in this room, so it is
    created 0600 and never leaves the machine. Nothing here is ever uploaded
    or committed -- .gitignore covers it, and the panel can clear it.
    """
    entry["ts"] = datetime.now().isoformat(timespec="seconds")
    entry["via"] = "live"
    fd = os.open(HISTORY, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps(entry) + "\n")


class Live:
    def __init__(self, once=False):
        self.once = once
        self.stop = asyncio.Event()
        self.out_q = asyncio.Queue()
        self.said, self.heard, self.actions = [], [], []
        self.last_played = 0.0

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
                sc = getattr(msg, "server_content", None)

                if getattr(msg, "data", None) and not MUTE.exists():
                    self.last_played = time.monotonic()   # close the mic the moment audio arrives
                    self.out_q.put_nowait(msg.data)   # dropped while the tour narrates

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

                    if getattr(sc, "turn_complete", None):
                        heard = "".join(self.heard).strip()
                        said = "".join(self.said).strip()
                        if heard or said or self.actions:
                            log({"heard": heard, "reply": said, "actions": self.actions})
                            print(f"  you: {heard}\n  beckon: {said}"
                                  + (f"\n  ran: {[a['tool'] for a in self.actions]}"
                                     if self.actions else ""))
                        self.heard, self.said, self.actions = [], [], []
                        if self.once:
                            self.stop.set()

                tc = getattr(msg, "tool_call", None)
                if tc and getattr(tc, "function_calls", None):
                    calls = list(tc.function_calls)
                    # Off the loop: a 45s screen read used to freeze mic, speaker
                    # and the socket pump for its whole duration.
                    results = await asyncio.to_thread(run_tools, calls)
                    responses = []
                    for call, result in zip(calls, results):
                        self.actions.append({"tool": call.name, "args": dict(call.args or {})})
                        responses.append(types.FunctionResponse(
                            id=call.id, name=call.name, response={"result": result}))
                    await session.send_tool_response(function_responses=responses)

    async def run(self):
        key = api_key()
        if not key:
            notify("No API key set — open the Beckon panel", "critical")
            print("no API key", file=sys.stderr)
            return 1

        client = genai.Client(api_key=key)
        try:   # be less trigger-happy about faint bleed-through counting as speech
            vad = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_LOW))
        except AttributeError:
            vad = None
        known = memory.render()
        config = types.LiveConnectConfig(
            realtime_input_config=vad,
            response_modalities=["AUDIO"],
            system_instruction=SYSTEM + ("\n\n" + known if known else ""),
            tools=[{"function_declarations": declarations()}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE))),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # let the API manage context instead of us trimming by hand
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()),
        )

        PIDFILE.write_text(str(os.getpid()))
        MUTE.unlink(missing_ok=True)   # a cancelled tour must not leave us deaf
        notify("Listening — talk to me", "low")
        print(f"Beckon live [{MODEL}, voice {VOICE}] — Ctrl+C to stop\n")

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
            PIDFILE.unlink(missing_ok=True)
            MUTE.unlink(missing_ok=True)
            notify("Session ended", "low")
        return status


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

    live = Live(once=once)

    def bye(*_):
        live.stop.set()
    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)

    return asyncio.run(live.run())


if __name__ == "__main__":
    sys.exit(main())
