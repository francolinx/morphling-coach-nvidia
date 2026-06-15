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

try:
    import rag  # semantic RAG (ChromaDB + nomic-embed-text); degrades on its own
except Exception:  # pragma: no cover - rag should always import, but never hard-fail
    rag = None

try:
    import memory  # episodic per-match memory (self-evolving coach)
except Exception:  # pragma: no cover
    memory = None

try:
    import openshell_sandbox as _sandbox  # OpenShell audit logging
except Exception:  # pragma: no cover
    _sandbox = None

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


def _match_query(match: dict) -> str:
    """Build a natural-language retrieval query from the match data."""
    p = match.get("player", {})
    lp = match.get("lane_phase", {})
    enemy_mid = lp.get("enemy_mid", "")
    enemies = ", ".join(match.get("enemy_lineup", []))
    death_ctx = " ".join(d.get("context", "") for d in match.get("deaths", []))
    return (
        f"{p.get('hero', 'morphling')} {p.get('role', 'mid')} laning analysis, "
        f"last hits, deaths and itemization vs enemy mid {enemy_mid}; "
        f"enemy lineup {enemies}; mistakes: {death_ctx}"
    ).strip()


def _match_phase(match: dict) -> str:
    """Pick the most relevant laning phase bucket for biasing retrieval."""
    deaths_early = match.get("lane_phase", {}).get("deaths_0_10min", 0)
    return "laning_6_10min" if deaths_early else "laning_3_6min"


# Maps enemy heroes to the matchup-corpus files that cover them, so the
# Draft & Counter Analysis section is always grounded in real corpus text.
_MATCHUP_HINTS = {
    "viper": "ranged_carries", "drow_ranger": "ranged_carries", "luna": "ranged_carries",
    "shadow_fiend": "shadowfiend", "shadowfiend": "shadowfiend",
    "invoker": "invoker", "lina": "lina", "storm_spirit": "storm_spirit", "doom": "doom",
    "lion": "silence_heroes", "silencer": "silence_heroes", "skywrath_mage": "silence_heroes",
    "phantom_assassin": "melee_carries", "juggernaut": "melee_carries", "anti_mage": "melee_carries",
    "zeus": "burst_mages", "lich": "burst_mages", "tinker": "burst_mages",
}


def _ensure_draft_coverage(match: dict, hits: list, max_total: int = 7):
    """Guarantee matchup chunks for the enemy lineup are in context for the
    Draft & Counter Analysis section, appending them if RAG ranking missed them."""
    have = {h.get("id") for h in hits}
    present_names = {h.get("name", "") for h in hits}
    enemy = [h.lower() for h in match.get("enemy_lineup", [])]
    wanted_files = {f"vs_{_MATCHUP_HINTS[e]}" for e in enemy if e in _MATCHUP_HINTS}
    missing = wanted_files - present_names
    if not missing:
        return hits

    try:
        corpus = rag.load_chunks() if rag is not None else None
    except Exception:
        corpus = None
    if corpus is None:
        corpus = [{"id": f"{c['phase']}/{c['name']}#0", "phase": c["phase"],
                   "name": c["name"], "title": c["name"], "text": c["text"]}
                  for c in load_corpus()]

    for fname in missing:
        for c in corpus:
            if c["name"] == fname and c["id"] not in have:
                hits.append(c)
                have.add(c["id"])
                break
    return hits[:max_total]


def get_corpus_context(match: dict, top_k: int = 5):
    """Semantic RAG retrieval with a keyword fallback baked in.

    Tries rag.retrieve() (ChromaDB + embeddings). If the rag module itself is
    missing, falls back to the local keyword heuristic so the demo never fails.
    Always tops up matchup coverage for the enemy lineup.
    """
    hits = None
    if rag is not None:
        try:
            hits = rag.retrieve(_match_query(match), phase=_match_phase(match), top_k=top_k)
        except Exception:
            hits = None
    if not hits:
        chunks = load_corpus()
        hits = retrieve(chunks, query="laning analysis", match=match, max_chunks=top_k)
        # normalize keyword-fallback shape to include an id
        for c in hits:
            c.setdefault("id", f"{c.get('phase','')}/{c.get('name','')}#0")

    return _ensure_draft_coverage(match, list(hits))


def retrieve(chunks, query: str, match: dict, max_chunks: int = 5):
    """Heuristic keyword retrieval — the ultimate fallback if rag is unavailable.

    Kept deliberately simple; rag.py is the primary semantic path now.
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

Use ONLY the provided match data, corpus context, and episodic memory. Do not \
invent statistics, item timings, hero abilities, or tactical details that are \
not in the input. Every claim about the enemy draft must come from the \
enemy_lineup/ally_lineup fields and the matchups corpus — never from imagined \
screen reading.

Produce coaching output in this EXACT structure, all six sections, clean markdown:

## Lane Phase Review
Two to three sentences comparing actual performance against laning benchmarks. \
Be specific with numbers from the match data (last hits at 5/10, deaths, GPM/XPM).

## Death Analysis
For each death, identify the decision error and what should have been done \
differently. Reference the time and context from the match data.

## Item Timing
Identify the single most impactful item timing miss and the window it should \
have landed in.

## Draft & Counter Analysis
Using enemy_lineup and ally_lineup from the match data PLUS the matchups corpus, \
tell the player how to itemize and play against THIS enemy draft. Name specific \
enemy heroes and the concrete counter-play (e.g. "Viper + Drow want a long game; \
rush Manta by 16:00 and have BKB before fighting into Viper's ultimate"). \
Ground every recommendation in the listed heroes — this is structured \
counter-intel, not guesswork.

## Three Practice Goals
Numbered list of three specific, measurable goals for the next match.

## Coach Memory Note
One sentence summarizing the player's primary growth area. If episodic memory is \
provided, explicitly note whether the recurring mistake is improving or persisting.

Tone: Direct, specific, professional. This is competitive coaching."""


def build_prompt(match: dict, corpus_chunks: list, memory_context: str = "") -> str:
    corpus_text = "\n\n---\n\n".join(
        f"### {c['name']} ({c['phase']})\n{c['text']}"
        for c in corpus_chunks
    )
    memory_block = ""
    if memory_context:
        memory_block = f"""=== Episodic memory: YOU HAVE COACHED THIS PLAYER BEFORE ===
{memory_context}

You are not meeting this player for the first time. Reference these prior \
patterns directly in your analysis — call out whether the recurring mistakes \
above are improving or persisting in THIS match, and make the Coach Memory Note \
explicitly build on this history (e.g. "Same overextending pattern as your last \
two games"). Do not ignore this section.

"""
    return f"""=== Match data ===
{json.dumps(match, indent=2)}

=== Corpus context (Morphling knowledge base, patch 7.41b) ===
{corpus_text}

{memory_block}Produce the coaching review now."""


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

    # Audit the model call through OpenShell (proves all inference is local)
    if _sandbox is not None:
        try:
            _sandbox.log_model_call(
                endpoint=endpoint,
                model=MODEL_NAME,
                prompt_chars=sum(len(m.get("content", "")) for m in messages),
            )
        except Exception:
            pass

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
    """Analyze a single match and return coaching markdown.

    Self-evolving loop: load prior-session memory, fold it into the prompt,
    generate coaching, then persist this session so the next call is smarter.
    """
    relevant = get_corpus_context(match)

    # --- Episodic memory: what do we know from past sessions? ---
    player = match.get("player", {})
    hero = player.get("hero", "morphling")
    role = player.get("role", "mid")
    # NOTE: do NOT exclude the current match_id. The current match is stored
    # AFTER we build context, so it can't leak into its own first run — but a
    # SECOND run of the same match SHOULD see the prior session. Excluding by
    # match_id silently wiped the only record on a rerun (the demo bug).
    memory_context = ""
    if memory is not None:
        try:
            memory_context = memory.build_memory_context(hero=hero, role=role)
        except Exception:
            memory_context = ""

    audit({
        "event": "coaching_start",
        "match_id": match.get("match_id"),
        "model": MODEL_NAME,
        "corpus_chunks": [c["name"] for c in relevant],
        "has_memory_context": bool(memory_context),
    })

    user_prompt = build_prompt(match, relevant, memory_context=memory_context)

    start = time.time()
    response = call_local_model(SYSTEM_PROMPT, user_prompt, timeout=TIMEOUT)
    latency_ms = int((time.time() - start) * 1000)

    audit({
        "event": "coaching_complete",
        "match_id": match.get("match_id"),
        "latency_ms": latency_ms,
        "response_chars": len(response),
    })

    # --- Persist this session to episodic memory (self-evolving story) ---
    if memory is not None:
        try:
            memory.store_session(match, response)
        except Exception:
            pass

    # Legacy flat memory note (kept for back-compat with v0 tooling)
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
