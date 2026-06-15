"""voice.py — ReplaySense voice coach (local, hands-free Q&A).

Ask the coach out loud ("what should I work on?") and hear the answer, or type
the question for a no-audio demo. Everything stays on the box.

DESIGN NOTE — immune to the 8642 bug by construction:
  This module reads REPLAYSENSE_MODEL_URL / REPLAYSENSE_MODEL_NAME and DEFAULTS
  to the real local Ollama (http://localhost:11434/api/chat, gemma4:latest) when
  they are unset. It NEVER defaults to the never-installed Hermes :8642 endpoint,
  and it does not inherit agent.py's module-level default. On a connection
  failure it logs one clear line (the URL it tried + "model endpoint
  unreachable") and exits cleanly — it never hangs silently.

LIVE-DEMO HARDENING (item 4):
  Each external dependency — microphone, STT model load, TTS engine load, and the
  model endpoint call — is wrapped in its own try/except. If one fails we print a
  single legible line naming the failing component and stop gracefully; we never
  crash with a raw traceback or hang. The text path (--text / --no-speak) needs
  only `requests`, so it is the guaranteed fallback if audio hardware misbehaves.
"""

import argparse
import os
import sys
import tempfile
import time
import wave

import requests

# ---------------------------------------------------------------------------
# CONFIG — correct local defaults, overridable by env. NEVER 8642.
# ---------------------------------------------------------------------------
DEFAULT_MODEL_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL_NAME = "gemma4:latest"

MODEL_URL = os.environ.get("REPLAYSENSE_MODEL_URL", DEFAULT_MODEL_URL)
MODEL_NAME = os.environ.get("REPLAYSENSE_MODEL_NAME", DEFAULT_MODEL_NAME)
API_KEY = os.environ.get("REPLAYSENSE_API_KEY", "")
TIMEOUT = int(os.environ.get("REPLAYSENSE_TIMEOUT", "120"))

# STT / audio knobs (all overridable; safe defaults for a laptop mic).
STT_MODEL_SIZE = os.environ.get("REPLAYSENSE_STT_MODEL", "base")
SAMPLE_RATE = int(os.environ.get("REPLAYSENSE_MIC_RATE", "16000"))
RECORD_SECONDS = int(os.environ.get("REPLAYSENSE_MIC_SECONDS", "6"))


# ---------------------------------------------------------------------------
# GRACEFUL FAILURE — one legible line, clean exit. Never a raw traceback.
# ---------------------------------------------------------------------------
def fail(component: str, detail: str, code: int = 1):
    """Print '[voice] <component> unavailable: <detail>' and exit cleanly."""
    print(f"[voice] {component} unavailable: {detail}", file=sys.stderr)
    print("[voice] stopping gracefully — fix the component above, or run the "
          "text fallback:  python voice.py --text \"...\" --no-speak", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------------------
# MODEL CLIENT — self-contained, correct default, finite timeout (never hangs).
# ---------------------------------------------------------------------------
def _endpoint_and_kind(url: str):
    """Return (post_url, kind). Ollama iff '/api/chat'; else OpenAI-compatible."""
    u = url.rstrip("/")
    if "/api/chat" in u:
        return u, "ollama"
    if u.endswith("/chat/completions"):
        return u, "openai"
    if u.endswith("/v1"):
        return u + "/chat/completions", "openai"
    if "/v1" in u:
        return u.split("/v1")[0].rstrip("/") + "/v1/chat/completions", "openai"
    return u, "openai"


def _extract(data: dict) -> str:
    """Pull assistant text out of either OpenAI or Ollama schema."""
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") or {}
        if msg.get("content"):
            return msg["content"]
        if choices[0].get("text"):
            return choices[0]["text"]
    msg = data.get("message")
    if isinstance(msg, dict) and msg.get("content"):
        return msg["content"]
    if data.get("response"):
        return data["response"]
    return ""


def call_model(system: str, user: str, timeout: int = None) -> str:
    """Call the local model. On connection failure, log one line and exit."""
    if timeout is None:
        timeout = TIMEOUT
    post_url, kind = _endpoint_and_kind(MODEL_URL)
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if kind == "openai" and API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"

    try:
        r = requests.post(post_url, json=payload, headers=headers, timeout=timeout)
        r.raise_for_status()
    except requests.exceptions.ConnectionError:
        fail("model endpoint", f"{post_url} model endpoint unreachable "
             "(is Ollama running? `ollama serve`)")
    except requests.exceptions.Timeout:
        fail("model endpoint", f"{post_url} timed out after {timeout}s "
             "(model endpoint unreachable / too slow)")
    except requests.exceptions.HTTPError as e:
        fail("model endpoint", f"{post_url} returned HTTP error: {e}")
    return _extract(r.json())


# ---------------------------------------------------------------------------
# COACHING PROMPT — grounded in episodic memory when available.
# ---------------------------------------------------------------------------
VOICE_SYSTEM = (
    "You are ReplaySense, a local voice coach for a competitive Dota 2 Morphling "
    "mid player. Answer the player's spoken question directly and concisely — two "
    "to four sentences, conversational, no markdown headers or bullet lists, since "
    "this will be read aloud. Be specific and actionable. Use the player's session "
    "history below when relevant; do not invent stats that aren't given."
)


def build_system_prompt() -> str:
    """Fold episodic memory into the system prompt, guarded so it never crashes."""
    try:
        import memory  # local module; optional
        ctx = memory.build_memory_context("morphling", "mid")
        if ctx:
            return VOICE_SYSTEM + "\n\nPlayer session history:\n" + ctx
    except Exception:
        pass  # memory is a nice-to-have; never block the voice path on it
    return VOICE_SYSTEM


def ask_coach(question: str, timeout: int = None) -> str:
    """Send a spoken/typed question to the coach and return the reply text."""
    reply = call_model(build_system_prompt(), question.strip(), timeout=timeout)
    if not reply or not reply.strip():
        fail("model endpoint", f"{MODEL_URL} returned an empty response")
    return reply.strip()


# ---------------------------------------------------------------------------
# MICROPHONE — guarded; failure is legible, not a hang.
# ---------------------------------------------------------------------------
def record_audio(seconds: int = None, rate: int = None) -> str:
    """Record `seconds` of mic audio to a temp WAV path. Guards mic + deps."""
    seconds = seconds or RECORD_SECONDS
    rate = rate or SAMPLE_RATE
    try:
        import numpy as np
        import sounddevice as sd
    except Exception as e:
        fail("microphone (sounddevice/numpy)",
             f"{type(e).__name__}: {e}. Install with `pip install sounddevice numpy`")

    try:
        print(f"[voice] recording {seconds}s — speak now...", flush=True)
        audio = sd.rec(int(seconds * rate), samplerate=rate, channels=1, dtype="int16")
        sd.wait()
    except Exception as e:
        fail("microphone capture", f"{type(e).__name__}: {e} "
             "(no input device? check OS mic permissions)")

    try:
        path = os.path.join(tempfile.gettempdir(), f"replaysense_voice_{int(time.time())}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16
            wf.setframerate(rate)
            wf.writeframes(audio.tobytes())
        return path
    except Exception as e:
        fail("audio write", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# SPEECH-TO-TEXT — guarded model load + transcription.
# ---------------------------------------------------------------------------
_STT = {"engine": None, "kind": None}


def load_stt():
    """Load an STT backend once (faster-whisper preferred, then whisper)."""
    if _STT["engine"] is not None:
        return _STT
    try:
        from faster_whisper import WhisperModel
        _STT["engine"] = WhisperModel(STT_MODEL_SIZE, device="auto", compute_type="int8")
        _STT["kind"] = "faster_whisper"
        return _STT
    except Exception:
        pass
    try:
        import whisper
        _STT["engine"] = whisper.load_model(STT_MODEL_SIZE)
        _STT["kind"] = "whisper"
        return _STT
    except Exception as e:
        fail("speech-to-text model",
             f"could not load faster-whisper or whisper ({type(e).__name__}: {e}). "
             "Install one: `pip install faster-whisper`")


def transcribe(wav_path: str) -> str:
    """Transcribe a WAV file to text. Guards the inference call."""
    stt = load_stt()
    try:
        if stt["kind"] == "faster_whisper":
            segments, _ = stt["engine"].transcribe(wav_path)
            return " ".join(seg.text for seg in segments).strip()
        return stt["engine"].transcribe(wav_path).get("text", "").strip()
    except Exception as e:
        fail("speech-to-text transcription", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# TEXT-TO-SPEECH — guarded engine load + playback.
# ---------------------------------------------------------------------------
_TTS = {"engine": None}


def load_tts():
    """Load an offline TTS engine (pyttsx3 / SAPI5 on Windows)."""
    if _TTS["engine"] is not None:
        return _TTS["engine"]
    try:
        import pyttsx3
        _TTS["engine"] = pyttsx3.init()
        return _TTS["engine"]
    except Exception as e:
        fail("text-to-speech engine",
             f"{type(e).__name__}: {e}. Install with `pip install pyttsx3`, "
             "or run with --no-speak to print the reply instead")


def speak(text: str):
    """Speak text aloud. Guards playback so a TTS hiccup can't crash the demo."""
    engine = load_tts()
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        # Don't hard-exit here — we already have the text; degrade to printing.
        print(f"[voice] text-to-speech playback failed ({type(e).__name__}: {e}); "
              "printing reply instead.", file=sys.stderr)


# ---------------------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------------------
def run_once(question_text: str = None, use_mic: bool = False,
             speak_reply: bool = True, timeout: int = None) -> str:
    """One full turn: (mic->STT | text) -> coach -> (TTS | print)."""
    if use_mic:
        wav = record_audio()
        question = transcribe(wav)
        try:
            os.remove(wav)
        except OSError:
            pass
        if not question:
            print("[voice] heard nothing — try again or use --text.", file=sys.stderr)
            return ""
        print(f"[voice] you asked: {question}")
    else:
        question = (question_text or "").strip()
        if not question:
            fail("input", "no question provided (use --text \"...\" or --listen)")

    reply = ask_coach(question, timeout=timeout)
    print(f"\nCoach: {reply}\n")
    if speak_reply:
        speak(reply)
    return reply


def main():
    parser = argparse.ArgumentParser(description="ReplaySense local voice coach")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--text", metavar="Q", help="ask a typed question (no mic needed)")
    src.add_argument("--listen", action="store_true", help="capture the question from the microphone")
    parser.add_argument("--loop", action="store_true",
                        help="keep answering (press Enter to record / type each turn; Ctrl-C to quit)")
    parser.add_argument("--no-speak", action="store_true", help="print the reply instead of speaking it")
    parser.add_argument("--timeout", type=int, default=TIMEOUT, help="model timeout seconds (no silent hang)")
    args = parser.parse_args()

    speak_reply = not args.no_speak
    use_mic = args.listen and not args.text

    print(f"[voice] ReplaySense voice coach · model `{MODEL_NAME}` @ {MODEL_URL}")
    if "8642" in MODEL_URL:
        # Defensive: respect an explicit override, but warn loudly — 8642 was never installed.
        print("[voice] WARNING: MODEL_URL points at :8642 (Hermes was never installed). "
              "Set REPLAYSENSE_MODEL_URL to http://localhost:11434/api/chat.", file=sys.stderr)

    try:
        if not args.loop:
            run_once(question_text=args.text, use_mic=use_mic,
                     speak_reply=speak_reply, timeout=args.timeout)
            return
        # Continuous push-to-talk / type loop — no VAD, so it never hangs waiting.
        while True:
            if use_mic:
                input("[voice] press Enter to record your question (Ctrl-C to quit)... ")
                run_once(use_mic=True, speak_reply=speak_reply, timeout=args.timeout)
            else:
                q = input("Ask the coach (blank + Enter to quit): ").strip()
                if not q:
                    break
                run_once(question_text=q, speak_reply=speak_reply, timeout=args.timeout)
    except KeyboardInterrupt:
        print("\n[voice] bye.")


if __name__ == "__main__":
    main()
