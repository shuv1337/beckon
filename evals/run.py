"""Run evals/cases.jsonl against a real Live session with a fake desktop.

    python3 evals/run.py                         # current defaults, text input
    python3 evals/run.py --input audio           # TTS the utterance, stream it
    python3 evals/run.py --cases 'mem_*,kb_*' --repeat 3
    python3 evals/run.py --model gemini-3.8-live --thinking low --non-blocking

Each case opens a fresh session (clean context), sends the utterance, answers
tool calls from the FakeDesktop, and records the transcript, calls, timing and
token usage. Results land in evals/results/<stamp>-<tag>.json; compare two
runs with evals/compare.py. Network is required; this is not part of pytest.
"""
import argparse
import asyncio
import base64
import fnmatch
import json
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "beckon"))

import common                                   # noqa: E402
import live                                     # noqa: E402
import memory                                   # noqa: E402
import narrate                                  # noqa: E402
from google import genai                        # noqa: E402
from google.genai import types                  # noqa: E402

from evals.fake_desktop import FIXTURES, FakeDesktop   # noqa: E402
from evals import score as scoring                     # noqa: E402

CASES = ROOT / "evals" / "cases.jsonl"
RESULTS = ROOT / "evals" / "results"
AUDIO_CACHE = Path.home() / ".cache" / "beckon" / "eval"
USER_VOICE = "Charon"          # distinct from the assistant's voice
PCM_RATE = 24000               # narrate.generate output; Live resamples for us
# Bare imperatives ("set the volume to fifty percent") trip the TTS endpoint's
# PROHIBITED_CONTENT filter; framed as text to be read aloud they pass. Several
# frames because TTS sometimes mis-renders ("lock the screen" came out as "walk
# the screen"); a clip that fails the read-back check is regenerated under the
# next frame, which changes the cache key.
TTS_FRAMES = ("Read this aloud in a casual voice, exactly as written: ",
              "Say the following sentence clearly, exactly as written: ",
              "Speak this, word for word: ")


def load_cases(patterns):
    cases = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]
    if patterns:
        pats = [p.strip() for p in patterns.split(",") if p.strip()]
        cases = [c for c in cases
                 if any(fnmatch.fnmatch(c["id"], p) or fnmatch.fnmatch(c["cat"], p) for p in pats)]
    return cases


def _words(s):
    return ["".join(ch for ch in w.lower() if ch.isalnum()) for w in str(s).split()]


def matches_utterance(want, heard):
    """Did the clip say what we meant? Every word of a short utterance must be
    heard; longer ones tolerate one miss. Numbers are compared as words (the
    transcription prompt asks for that), so 'fifty' vs '50' is a miss and the
    clip gets regenerated rather than silently drifting."""
    w = [x for x in _words(want) if x]
    hw = [x for x in _words(heard) if x]
    if not w:
        return True
    if "".join(w) == "".join(hw):
        return True            # "fullscreen this" vs "full screen this" is faithful
    h = set(hw)
    hits = sum(1 for x in w if x in h)
    return hits == len(w) if len(w) <= 4 else hits >= len(w) - 1


def transcribe(wav_path):
    b64 = base64.b64encode(wav_path.read_bytes()).decode()
    d = common.generate_content(common.setting("text_model"), {"contents": [{"role": "user", "parts": [
        {"text": "Transcribe this audio exactly, word for word, writing any numbers as words. "
                 "Reply with only the transcript."},
        {"inline_data": {"mime_type": "audio/wav", "data": b64}}]}]})
    return (common.answer_text(d) or "").strip()


def utterance_wav(text):
    """A verified TTS rendering of `text` (24 kHz WAV via the tour's TTS path).
    Each clip is read back once with the text model; a clip that does not say
    the utterance is marked .bad and the next frame is tried. Verdicts are
    cached next to the clip so a run only pays for new utterances."""
    old = narrate.CACHE
    narrate.CACHE = AUDIO_CACHE
    best = None
    try:
        for frame in TTS_FRAMES:
            wav = narrate.generate(frame + text, "say", USER_VOICE)
            if not wav:
                continue
            ok, bad = wav.with_suffix(".ok"), wav.with_suffix(".bad")
            if ok.exists():
                return wav
            if bad.exists():
                best = best or wav
                continue
            heard = transcribe(wav)
            if matches_utterance(text, heard):
                ok.write_text(heard)
                return wav
            bad.write_text(heard)
            print(f"  tts mis-rendered {text!r} as {heard!r}; retrying", file=sys.stderr)
            best = best or wav
    finally:
        narrate.CACHE = old
    if best:
        print(f"  warning: no frame rendered {text!r} faithfully; using first clip", file=sys.stderr)
        return best
    raise RuntimeError("TTS failed for utterance")


def utterance_pcm(text):
    """24 kHz mono 16-bit PCM for `text`."""
    b = utterance_wav(text).read_bytes()
    return b[44:] if b[:4] == b"RIFF" else b


async def send_text(session, text):
    await session.send_client_content(
        turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)


async def send_audio(session, text, pace=1.0, tail=1.5):
    """Stream the TTS utterance as the mic would: real-time paced 100 ms
    chunks, then `tail` seconds of silence, then audio_stream_end. Short clips
    ("thanks, that's all") were missed by VAD when sent at 2x with a 1 s tail,
    so pace=1 is the default; it is what a real user sounds like anyway."""
    pcm = utterance_pcm(text)
    chunk = PCM_RATE * 2 // 10                         # 100 ms
    mime = f"audio/pcm;rate={PCM_RATE}"
    for i in range(0, len(pcm), chunk):
        await session.send_realtime_input(audio=types.Blob(data=pcm[i:i + chunk], mime_type=mime))
        await asyncio.sleep(0.1 * pace)
    silence = b"\0" * int(PCM_RATE * 2 * tail)
    for i in range(0, len(silence), chunk):
        await session.send_realtime_input(audio=types.Blob(data=silence[i:i + chunk], mime_type=mime))
        await asyncio.sleep(0.1 * pace)
    await session.send_realtime_input(audio_stream_end=True)


async def one_turn(session, fd, opt):
    """Drain the model's response to one utterance. Mirrors Live.receive:
    tool calls are answered inline; the pass ends when the SDK sees the turn
    complete. If a tool call was answered after the last turn_complete, take
    one more short pass so a filler-then-act reply isn't cut off."""
    heard, said, calls, usage = [], [], [], None
    t0 = time.monotonic()
    deadline = t0 + opt.timeout
    first_response = None
    last_complete = last_tool = None

    async def one_pass(idle):
        """One session.receive() pass. Returns True when the SDK ended it
        (turn complete), False on idle timeout or when the case deadline
        passes -- never raises, so what was heard so far is kept."""
        nonlocal usage, first_response, last_complete, last_tool
        gen = session.receive().__aiter__()
        while True:
            wait = min(idle, deadline - time.monotonic())
            if wait <= 0:
                return False
            try:
                msg = await asyncio.wait_for(gen.__anext__(), wait)
            except StopAsyncIteration:
                return True
            except asyncio.TimeoutError:
                return False
            sc = getattr(msg, "server_content", None)
            tc = getattr(msg, "tool_call", None)
            if first_response is None and (sc or tc):
                first_response = time.monotonic()
            um = getattr(msg, "usage_metadata", None)
            if um:
                usage = live.usage_dict(um)
            if sc:
                it = getattr(sc, "input_transcription", None)
                if it and getattr(it, "text", None):
                    heard.append(it.text)
                ot = getattr(sc, "output_transcription", None)
                if ot and getattr(ot, "text", None):
                    said.append(ot.text)
                if live.turn_is_complete(sc):
                    last_complete = time.monotonic()
            if tc and getattr(tc, "function_calls", None):
                fcs = list(tc.function_calls)
                results = await asyncio.to_thread(live.run_tools, fcs, fd.tools)
                responses = []
                for call, r in zip(fcs, results):
                    calls.append({"tool": call.name, "args": dict(call.args or {}),
                                  "ms": r["ms"], "ok": r["ok"],
                                  "result": json.dumps(r["result"], default=str)[:300]})
                    responses.append(types.FunctionResponse(
                        id=call.id, name=call.name, response={"result": r["result"]}))
                await session.send_tool_response(function_responses=responses)
                last_tool = time.monotonic()

    # First pass: up to the case budget (a screen read can legitimately take a
    # while). Follow-up passes only wait `settle` seconds for a post-tool reply.
    # Keep going while last_complete is still None so a filler turn_complete
    # that ended the SDK stream does not cut off the real tool+IDLE turn.
    finished = await one_pass(opt.timeout)
    passes = 1
    while finished and (last_complete is None or (last_tool and last_tool > last_complete)) \
            and passes < 4 and time.monotonic() < deadline:
        finished = await one_pass(opt.settle)
        passes += 1
    return {
        "heard": "".join(heard).strip(), "reply": "".join(said).strip(), "calls": calls,
        "usage": usage, "turn_ms": int((time.monotonic() - t0) * 1000),
        "first_ms": int((first_response - t0) * 1000) if first_response else None,
        # a timeout is only a timeout if the turn never completed at all
        "timeout": not finished and last_complete is None,
    }


async def run_case(client, case, opt):
    fd = FakeDesktop(case["desktop"])
    with fd.installed():
        decls = live.declarations(fd.tools)
        config = live_config_for(opt, memory.render(), decls)
        row = {"id": case["id"], "cat": case["cat"], "say": case["say"], "turns": [],
               "calls": [], "reply": "", "heard": "", "usage": None, "turn_ms": 0,
               "timeout": False, "error": None}
        try:
            async with client.aio.live.connect(model=opt.model, config=config) as session:
                utterances = [case["say"]] + list(case.get("then", []))
                first_turn_calls = None
                for i, text in enumerate(utterances):
                    if opt.input == "audio":
                        await send_audio(session, text)
                    else:
                        await send_text(session, text)
                    t = await one_turn(session, fd, opt)
                    if i == 0:
                        first_turn_calls = [c["tool"] for c in t["calls"]]
                    row["turns"].append({"say": text, "heard": t["heard"], "reply": t["reply"],
                                         "turn_ms": t["turn_ms"], "first_ms": t["first_ms"],
                                         "timeout": t["timeout"]})
                    row["calls"] += t["calls"]
                    row["turn_ms"] += t["turn_ms"]
                    row["timeout"] = row["timeout"] or t["timeout"]
                    row["usage"] = t["usage"] or row["usage"]
                    if t["timeout"]:
                        break
                row["reply"] = row["turns"][-1]["reply"] if row["turns"] else ""
                row["heard"] = row["turns"][0]["heard"] if row["turns"] else ""
        except Exception as e:      # connection drop, quota, etc. -- score what we have
            row["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            first_turn_calls = None
    replies = [t["reply"] for t in row["turns"]]
    row.update(scoring.score(case, row["calls"], replies, first_turn_calls))
    if row["error"] or row["timeout"]:
        # an empty transcript must not satisfy no_tools / reply_max_words by default
        row["pass"] = False
        row["failed"].append("error: " + (row["error"] or "timeout"))
    return row


def live_config_for(opt, known, decls):
    """Build LiveConnectConfig the same way a real session does.

    thinking=None means "use the model's default" so evals do not inherit
    the user's settings.json. --non-blocking True forces the stamp; otherwise
    MODEL_CAPS decides.
    """
    return live.live_config(
        voice=opt.voice, known=known, tool_decls=decls,
        model=opt.model, thinking=opt.thinking,
        non_blocking=True if opt.non_blocking else None)


def git_rev():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, cwd=ROOT).stdout.strip()
    except OSError:
        return None


def print_table(rows, summary):
    for r in rows:
        mark = "PASS" if r["pass"] else "FAIL"
        tools_ = ",".join(c["tool"] for c in r["calls"]) or "-"
        extra = f"  ! {r['error']}" if r.get("error") else ""
        fails = f"  [{'; '.join(r['failed'])}]" if r["failed"] else ""
        print(f"{mark} {r['id']:<28} {r['turn_ms']:>6}ms  {tools_[:60]}{fails}{extra}")
    print()
    print(f"pass {summary['pass_rate']}%  ({summary['cases']} runs)  "
          f"avg {summary['avg_turn_ms']}ms  avg reply {summary['avg_reply_words']} words  "
          f"forbid violations {summary['forbid_violations']}  tool failures {summary['tool_failures']}  "
          f"timeouts {summary['timeouts']}  errors {summary['errors']}  tokens {summary['total_tokens']}")
    for cat, s in summary["by_cat"].items():
        print(f"  {cat:<12} {s['pass_rate']:>3}%  (n={s['n']})")


async def main_async(opt):
    key = common.api_key()
    if not key:
        sys.exit("no API key (~/.config/beckon/api_key)")
    cases = load_cases(opt.cases)
    if not cases:
        sys.exit("no cases matched")
    fixtures = {p.stem for p in FIXTURES.glob("*.json")}
    for c in cases:
        bad = scoring.validate_case(c, fixtures, set(FakeDesktop(c["desktop"]).tools))
        if bad:
            sys.exit(f"{c['id']}: {bad}")
    client = genai.Client(api_key=key)
    rows = []
    for rep in range(opt.repeat):
        for case in cases:
            row = await run_case(client, case, opt)
            row["rep"] = rep
            rows.append(row)
            if opt.verbose:
                print(f"{'PASS' if row['pass'] else 'FAIL'} {row['id']}: {row['reply'][:100]!r}"
                      + (f"  {row['failed']}" if row["failed"] else ""), flush=True)
            await asyncio.sleep(opt.gap)
    summary = scoring.summarize(rows)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = opt.tag or f"{opt.model}-{opt.input}" + (f"-{opt.thinking}" if opt.thinking else "")
    out = RESULTS / f"{stamp}-{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "meta": {"provider": opt.provider, "model": opt.model, "voice": opt.voice,
                 "thinking": opt.thinking, "non_blocking": opt.non_blocking, "input": opt.input,
                 "repeat": opt.repeat, "cases": opt.cases, "stamp": stamp, "git": git_rev(),
                 "text_model": common.setting("text_model")},
        "summary": summary, "rows": rows}, indent=1, default=str))
    print_table(rows, summary)
    print(f"\nwrote {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--provider", default="gemini", choices=["gemini"],
                    help="only gemini until PLAN.md phase 7/8")
    # DEFAULTS, not live.MODEL / live.VOICE: those inherit settings.json.
    ap.add_argument("--model", default=common.DEFAULTS["model"])
    ap.add_argument("--voice", default=common.DEFAULTS["voice"])
    ap.add_argument("--thinking", default=None, choices=[None, "low", "medium", "high"],
                    help="thinking_level (models that support it)")
    ap.add_argument("--non-blocking", action="store_true",
                    help="declare tools NON_BLOCKING (required by *-extended-thinking)")
    ap.add_argument("--input", default="text", choices=["text", "audio"])
    ap.add_argument("--cases", default="", help="comma-separated globs on id or cat")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=60, help="seconds per turn")
    ap.add_argument("--settle", type=float, default=6, help="seconds to wait for a post-tool reply")
    ap.add_argument("--gap", type=float, default=0.5, help="seconds between sessions")
    ap.add_argument("--tag", default="", help="results filename suffix")
    ap.add_argument("-v", "--verbose", action="store_true")
    opt = ap.parse_args()
    asyncio.run(main_async(opt))


if __name__ == "__main__":
    main()
