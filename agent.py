"""ReplaySense agent: local-first Dota 2 coach.

Talks to a LOCAL LLM endpoint. The endpoint type is auto-detected from
REPLAYSENSE_MODEL_URL so the same code drives any local backend:

  * Hermes / OpenAI-compatible  (URL contains '/v1')  -> POST /v1/chat/completions
  * Ollama                      (URL contains '/api/chat') -> POST /api/chat
  * anything ambiguous          -> treated as OpenAI-compatible

Primary target is Hermes on http://localhost:8642/v1 (bearer auth via
REPLAYSENSE_API_KEY). Ollama on http://localhost:11434/api/chat is the fallback.
No cloud SDKs — raw `requests` only, everything stays on the box.
"""

import json
import os
import time
from pathlib import Path

import requests

# ============================================================================
# CONFIG — driven entirely by env vars so the dashboard can point us anywhere
# ============================================================================
MODEL_URL = os.environ.get("REPLAYSENSE_MODEL_URL", "http://localhost:8642/v1")
MODEL_NAME = os.environ.get("REPLAYSENSE_MODEL_NAME", "hermes")
API_KEY = os.environ.get("REPLAYSENSE_API_KEY", "")
# 120B is slow on first token; default generous and overridable via --timeout / env.
DEFAULT_TIMEOUT = int(os.environ.get("REPLAYSENSE_TIMEOUT", "180"))
TIMEOUT = DEFAULT_TIMEOUT

REPO_ROOT = Path(__file__).parent
CORPUS_DIR = REPO_ROOT / "corpus"
DATA_DIR = REPO_ROOT / "data"
MEMORY_PATH = REPO_ROOT / "coach_memory.json"
AUDIT_LOG = REPO_ROOT / "audit_log.jsonl"

# ============================================================================
# CORPUS LOADING — flat-file RAG (no vector DB for v0, just keyword match)
# ============================================================================
def load_corpus():
    """Load all 21 markdown files from corpus/ directory."""
    chunks = []
    for md_file in CORPUS_DIR.rglob("*.md"):
        phase = md_file.parent.name
        text = md_file.read_text(encoding="utf-8")
        chunks.append({
            "phase": phase,
            "name": md_file.stem,
            "text": text,
        })
    return chunks


def retrieve(chunks, query: str, match: dict, max_chunks: int = 5):
    """Pick the most relevant corpus chunks based on game state and query.

    For v0 we use simple heuristics rather than embeddings — it's faster to
    debug and the corpus is small (21 files).
    """
    selected = []

    # Always include general overview
    for c in chunks:
        if c["phase"] == "general" and "overview" in c["name"]:
            selected.append(c)
            break

    # Phase-relevant chunks (laning has 3 phases)
    for c in chunks:
        if c["phase"].startswith("laning_"):
            selected.append(c)

    # Matchup chunk if we know the enemy mid
    enemy_mid = match.get("lane_phase", {}).get("enemy_mid", "").lower()
    if enemy_mid:
        for c in chunks:
            if c["phase"] == "matchups" and enemy_mid in c["name"]:
                selected.append(c)
                break

    return selected[:max_chunks]


# ============================================================================
# PROMPT BUILDING
# ============================================================================
SYSTEM_PROMPT = """You are ReplaySense, a local AI coach for competitive Dota 2 \
players running entirely on the user's hardware. You are analyzing a match for \
a player who specializes in Morphling in the mid lane on patch 7.41b.

Use ONLY the provided match data and corpus context. Do not invent statistics, \
item timings, or tactical details that are not in the input.

Produce coaching output in this exact structure:

## Lane Phase Review
Two to three sentences comparing actual performance against laning benchmarks. \
Be specific with numbers from the match data.

## Death Analysis
For each death, identify the decision error and what should have been done \
differently.

## Item Timing
Identify the single most impactful item timing miss.

## Three Practice Goals
Numbered list of three specific, measurable goals for the next match.

## Coach Memory Note
One sentence summarizing the player's primary growth area.

Tone: Direct, specific, professional. This is competitive coaching."""


def build_prompt(match: dict, corpus_chunks: list) -> str:
    corpus_text = "\n\n---\n\n".join(
        f"### {c['name']} ({c['phase']})\n{c['text']}"
        for c in corpus_chunks
    )
    return f"""=== Match data ===
{json.dumps(match, indent=2)}

=== Corpus context (Morphling knowledge base, patch 7.41b) ===
{corpus_text}

Produce the coaching review now."""


# ============================================================================
# LOCAL MODEL CALL — multi-endpoint, auto-detected from the URL
# ============================================================================
def detect_endpoint_type(url: str) -> str:
    """Return 'ollama' or 'openai' based on the URL shape.

    Ollama is only chosen when the URL clearly points at /api/chat. Everything
    else (including bare hosts and explicit /v1 URLs) is OpenAI-compatible,
    which is our primary Hermes target.
    """
    u = url.lower()
    if "/api/chat" in u:
        return "ollama"
    return "openai"


def _openai_chat_url(url: str) -> str:
    """Normalize an OpenAI-compatible base URL to its chat-completions path."""
    u = url.rstrip("/")
    if u.endswith("/chat/completions"):
        return u
    if u.endswith("/v1"):
        return u + "/chat/completions"
    if "/v1" in u:  # e.g. .../v1/something unexpected — still aim at the standard path
        return u.split("/v1")[0].rstrip("/") + "/v1/chat/completions"
    return u + "/v1/chat/completions"


def call_local_model(system: str, user: str, timeout: int = None) -> str:
    """Call the configured local model endpoint and return the text content.

    Supports Ollama (/api/chat) and OpenAI-compatible (/v1/chat/completions)
    backends, auto-detected from MODEL_URL. Bearer auth is added for the
    OpenAI path when REPLAYSENSE_API_KEY is set.
    """
    if timeout is None:
        timeout = TIMEOUT

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kind = detect_endpoint_type(MODEL_URL)

    if kind == "ollama":
        endpoint = MODEL_URL
        payload = {"model": MODEL_NAME, "messages": messages, "stream": False}
        headers = {}
    else:  # openai-compatible (Hermes / NemoClaw / llama.cpp server / vLLM / NIM)
        endpoint = _openai_chat_url(MODEL_URL)
        payload = {"model": MODEL_NAME, "messages": messages, "stream": False}
        headers = {"Content-Type": "application/json"}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"

    r = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    return _extract_content(r.json())


def _extract_content(data: dict) -> str:
    """Pull the assistant text out of either schema, defensively."""
    # OpenAI-compatible: {"choices": [{"message": {"content": "..."}}]}
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if content:
            return content
        # Some servers stream-collapse into "text"
        if choices[0].get("text"):
            return choices[0]["text"]
    # Ollama: {"message": {"content": "..."}}
    msg = data.get("message")
    if isinstance(msg, dict) and msg.get("content"):
        return msg["content"]
    # Ollama /api/generate style fallback
    if data.get("response"):
        return data["response"]
    return ""


# ============================================================================
# COACH MEMORY
# ============================================================================
def save_memory(match_id: str, summary: str):
    memory = {}
    if MEMORY_PATH.exists():
        memory = json.loads(MEMORY_PATH.read_text())
    memory[match_id] = {
        "timestamp": time.time(),
        "growth_area": summary,
    }
    MEMORY_PATH.write_text(json.dumps(memory, indent=2))


def audit(event: dict):
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**event, "timestamp": time.time()}) + "\n")


# ============================================================================
# MAIN COACHING ENTRY POINT
# ============================================================================
def coach_match(match: dict) -> str:
    """Analyze a single match and return coaching markdown."""
    chunks = load_corpus()
    relevant = retrieve(chunks, query="laning analysis", match=match)

    audit({
        "event": "coaching_start",
        "match_id": match.get("match_id"),
        "model": MODEL_NAME,
        "corpus_chunks": [c["name"] for c in relevant],
    })

    user_prompt = build_prompt(match, relevant)

    start = time.time()
    response = call_local_model(SYSTEM_PROMPT, user_prompt, timeout=TIMEOUT)
    latency_ms = int((time.time() - start) * 1000)

    audit({
        "event": "coaching_complete",
        "match_id": match.get("match_id"),
        "latency_ms": latency_ms,
        "response_chars": len(response),
    })

    # Extract memory note from response
    if "Coach Memory Note" in response:
        memory_section = response.split("Coach Memory Note")[-1].strip()
        memory_note = memory_section.lstrip("#").strip().split("\n")[0]
        save_memory(match.get("match_id", "unknown"), memory_note)

    return response


if __name__ == "__main__":
    # CLI mode for quick testing
    import argparse

    parser = argparse.ArgumentParser(description="ReplaySense local Dota 2 coach")
    parser.add_argument("match", nargs="?", default="data/demo_match.json",
                        help="path to a match JSON file")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="per-request timeout in seconds (120B is slow on first token)")
    args = parser.parse_args()

    TIMEOUT = args.timeout
    match = json.loads(Path(args.match).read_text(encoding="utf-8"))
    print(coach_match(match))
