---
name: morphling-knowledge
description: Retrieve relevant Morphling / Dota 2 coaching knowledge (laning, itemization, matchups) from the local ReplaySense corpus for a given question or game situation.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [dota2, morphling, rag, coaching, replaysense]
    category: coaching
    requires_toolsets: [terminal]
    config:
      - key: replaysense.repo_root
        description: "Absolute path to the ReplaySense repo checkout (the directory containing corpus/ and agent.py). Exposed to the script as REPLAYSENSE_REPO."
        default: "/home/user/morphling-coach-nvidia"
        prompt: "Path to the ReplaySense repo checkout"
---

# Morphling Knowledge (ReplaySense RAG)

Use this skill whenever you need grounded, patch-7.41b Morphling/Dota knowledge —
laning fundamentals, item timings, or specific matchups — instead of answering
from memory. It returns the most relevant chunks from the local ReplaySense
corpus (21 markdown files) so you can reason over verified coaching content.

## When to use
- The user asks a Morphling/Dota question ("how do I survive vs a burst mage mid?").
- You are reviewing a match and need the corpus context for a phase or matchup.
- You need to ground a claim about itemization, spike timings, or lane trades.

## How to run
Call the helper script via the terminal toolset with the user's question:

```bash
python scripts/retrieve.py "how do I survive lane against a burst mage mid?"
```

Useful flags:
- `--max N` — return up to N chunks (default 4).
- `--json`  — machine-readable output ({query, chunks:[{phase,name,score,excerpt}]}).

The script auto-detects the repo (it walks up from its own location). If the
skill is installed outside the repo, point it at the checkout first:

```bash
REPLAYSENSE_REPO=/path/to/morphling-coach-nvidia python scripts/retrieve.py "manta timing"
```

## How it works
- Reuses `load_corpus()` from the repo's `agent.py` **read-only** to load the
  same corpus the coach uses (with a built-in fallback loader if `agent.py`'s
  dependencies aren't installed in the Hermes runtime).
- Ranks chunks by keyword overlap with the query, boosting filename/phase hits
  (e.g. a query mentioning "lina" surfaces `vs_lina`). It does not call the
  model, write memory, or touch the audit log.

## Using the output
Quote or paraphrase the returned excerpts as grounding. If the top result has a
low/zero score the query may be off-corpus — say so rather than inventing
details, consistent with the ReplaySense rule to use only provided context.
