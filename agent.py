"""ReplaySense agent: local-first Dota 2 coach.

Calls a local LLM endpoint (Ollama-compatible /api/chat). To swap to a different
local model (NIM, llama.cpp, etc), change MODEL_URL and MODEL_NAME below.
"""

import json
import os
import time
from pathlib import Path

import requests

# ============================================================================
# CONFIG — change these when your teammate's model is ready
# ============================================================================
MODEL_URL = os.environ.get("REPLAYSENSE_MODEL_URL", "http://localhost:11434/api/chat")
MODEL_NAME = os.environ.get("REPLAYSENSE_MODEL_NAME", "qwen3:8b")
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
# LOCAL MODEL CALL
# ============================================================================
def call_local_model(system: str, user: str) -> str:
    """Call Ollama-compatible local model endpoint."""
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
    }
    r = requests.post(MODEL_URL, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("content", "")


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
    response = call_local_model(SYSTEM_PROMPT, user_prompt)
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
    import sys
    match_path = sys.argv[1] if len(sys.argv) > 1 else "data/demo_match.json"
    match = json.loads(Path(match_path).read_text())
    print(coach_match(match))
