#!/usr/bin/env python3
"""Retrieve relevant Morphling/Dota coaching chunks from the ReplaySense corpus.

This is the helper script behind the `morphling-knowledge` Hermes skill. The
agent invokes it from SKILL.md via its terminal toolset:

    python scripts/retrieve.py "how do I survive lane vs a burst mage mid?"

It reuses the ReplaySense corpus loader (`load_corpus` in agent.py) read-only.
It does NOT import or touch the model / memory / audit code paths. If agent.py
cannot be imported (e.g. `requests` is not installed in the Hermes runtime),
it falls back to loading the corpus markdown directly with the same logic.

Query ranking is a small keyword-overlap scorer added here, because the
repo's existing `agent.retrieve()` is driven by live match-state rather than a
free-text query. We keep the corpus-loading behaviour identical to the repo.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "are", "how", "what", "when",
    "why", "who", "does", "did", "can", "should", "would", "could", "into",
    "from", "this", "that", "they", "them", "his", "her", "out", "off", "over",
    "get", "got", "vs", "dota", "morphling", "morph", "mid", "lane",
}


def find_repo_root() -> Path:
    """Locate the ReplaySense repo (the dir holding corpus/ and agent.py).

    Order: $REPLAYSENSE_REPO -> walk up from this script -> a few candidates.
    """
    env = os.environ.get("REPLAYSENSE_REPO")
    if env:
        p = Path(env).expanduser()
        if (p / "corpus").is_dir():
            return p

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "corpus").is_dir() and (parent / "agent.py").is_file():
            return parent

    for cand in (Path.cwd(), Path("/home/user/morphling-coach-nvidia")):
        if (cand / "corpus").is_dir():
            return cand

    sys.exit(
        "ERROR: could not locate the ReplaySense repo. Set REPLAYSENSE_REPO to "
        "the repo root (the directory containing corpus/ and agent.py)."
    )


def load_chunks(repo_root: Path):
    """Reuse agent.load_corpus() read-only; fall back to an equivalent loader."""
    sys.path.insert(0, str(repo_root))
    try:
        from agent import load_corpus  # read-only reuse of repo retrieval

        # agent.load_corpus uses module-level CORPUS_DIR rooted at the repo, so
        # this returns the same chunks the coach itself sees.
        return load_corpus()
    except Exception:
        # Fallback: identical behaviour to agent.load_corpus without importing.
        chunks = []
        for md_file in (repo_root / "corpus").rglob("*.md"):
            chunks.append({
                "phase": md_file.parent.name,
                "name": md_file.stem,
                "text": md_file.read_text(encoding="utf-8"),
            })
        return chunks
    finally:
        if sys.path and sys.path[0] == str(repo_root):
            sys.path.pop(0)


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2].strip()
    return text.strip()


def tokenize(query: str):
    toks = re.findall(r"[a-z0-9]+", query.lower())
    return [t for t in toks if len(t) >= 2 and t not in STOPWORDS]


def best_excerpt(body: str, tokens, limit: int = 700) -> str:
    """Return the paragraph with the most query-term hits, else the opener."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    if not paras:
        return body[:limit].strip()
    if tokens:
        scored = sorted(
            paras,
            key=lambda p: sum(p.lower().count(t) for t in tokens),
            reverse=True,
        )
        top = scored[0]
        if sum(top.lower().count(t) for t in tokens) > 0:
            return top[:limit].strip()
    return paras[0][:limit].strip()


def rank(chunks, query: str, max_chunks: int):
    tokens = tokenize(query)
    results = []
    for c in chunks:
        body = strip_frontmatter(c["text"])
        name_l = c["name"].lower()
        phase_l = c["phase"].lower()
        haystack = (name_l + " " + phase_l + " " + body.lower())
        score = sum(haystack.count(t) for t in tokens)
        # Boost direct hits in the filename / phase (e.g. "vs_lina", "matchups").
        score += 5 * sum(1 for t in tokens if t in name_l or t in phase_l)
        results.append((score, c, body))

    results.sort(key=lambda r: r[0], reverse=True)
    # If nothing matched, fall back to the general overview + earliest laning.
    if not tokens or results[0][0] == 0:
        results.sort(key=lambda r: (r[1]["phase"] != "general", r[1]["name"]))

    out = []
    for score, c, body in results[:max_chunks]:
        out.append({
            "phase": c["phase"],
            "name": c["name"],
            "score": int(score),
            "excerpt": best_excerpt(body, tokens),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Retrieve Morphling coaching corpus chunks.")
    ap.add_argument("query", nargs="+", help="Free-text query about Morphling / Dota.")
    ap.add_argument("--max", type=int, default=4, help="Max chunks to return (default 4).")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of markdown.")
    args = ap.parse_args()

    query = " ".join(args.query)
    repo_root = find_repo_root()
    chunks = load_chunks(repo_root)
    hits = rank(chunks, query, args.max)

    if args.json:
        print(json.dumps({"query": query, "chunks": hits}, indent=2))
        return

    print(f'# Morphling knowledge for: "{query}"')
    print(f"_Source: ReplaySense corpus ({len(chunks)} files, patch 7.41b)_\n")
    for h in hits:
        print(f"## {h['name']}  ({h['phase']})")
        print(h["excerpt"])
        print()
    print(f"_Returned {len(hits)} of {len(chunks)} corpus chunks._")


if __name__ == "__main__":
    main()
