"""Pre-generate tour narration as audio files.

The live model cannot reliably time its own speech against actions, so the tour
speaks from cached WAVs instead. Generated once, reused forever.
"""

import base64
import struct
import subprocess
from pathlib import Path

import common

CACHE = Path.home() / ".cache" / "beckon" / "narration"
VOICE = "Puck"


def _wav(pcm, rate=24000):
    """Wrap raw 16-bit mono PCM in a WAV header."""
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def generate(text, name, voice=VOICE):
    """Synthesise `text` to CACHE/name.wav if not already there. Returns the path."""
    import hashlib
    CACHE.mkdir(parents=True, exist_ok=True)
    name = f"{name}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"
    out = CACHE / f"{name}.wav"
    if out.exists() and out.stat().st_size > 2000:
        return out

    if not common.api_key():
        return None
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    import time
    for pause in (0, 3, 8):
        if pause:
            time.sleep(pause)      # the TTS endpoint rate-limits bursts
        try:
            d = common.generate_content(common.setting("tts_model"), body, timeout=90)
            part = d["candidates"][0]["content"]["parts"][0]["inlineData"]
            out.write_bytes(_wav(base64.b64decode(part["data"])))
            return out
        except Exception:
            continue
    return None


def duration(path):
    """Length of a WAV in seconds, from its header."""
    try:
        b = Path(path).read_bytes()
        rate = struct.unpack("<I", b[24:28])[0]
        return max(0.4, (len(b) - 44) / (rate * 2))
    except Exception:
        return 3.0


def play(path, block=True):
    if not path or not Path(path).exists():
        if block:
            import time
            time.sleep(3)
        return None
    p = subprocess.Popen(["paplay", str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if block:
        p.wait()
    return p
