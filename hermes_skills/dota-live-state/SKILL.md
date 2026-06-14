---
name: dota-live-state
description: Fetch the current live Dota 2 game state (clock, score, hero HP/mana, economy, items) from the local GSI relay and return a compact summary to reason over.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [dota2, gsi, live, replaysense]
    category: coaching
    requires_toolsets: [terminal]
    config:
      - key: replaysense.gsi_url
        description: "Live-state endpoint URL. Exposed to the script as DOTA_GSI_URL."
        default: "http://localhost:53000/latest"
        prompt: "Dota live-state endpoint URL"
---

# Dota Live State

Use this skill to read the **current** game state during a live match before you
give in-the-moment advice. It pulls from the local live-state relay and returns
a short, token-cheap summary (clock, score, hero HP/mana, level, economy, items).

## When to use
- The user asks "what should I do right now?" or references the live game.
- You need current numbers (clock/HP/net worth) rather than the post-game match
  file, before recommending a play, item, or fight decision.

## How to run
Call the helper script via the terminal toolset:

```bash
python scripts/live_state.py
```

Useful flags:
- `--json` — print the raw payload instead of the summary.
- `--url URL` — override the endpoint for this call.

Default endpoint is `http://localhost:53000/latest`. To point elsewhere:

```bash
DOTA_GSI_URL=http://localhost:53000/latest python scripts/live_state.py
```

## How it works
- GETs the endpoint with the stdlib only (no third-party deps), 5s timeout.
- Tolerant summarizer: understands Dota 2 GSI shapes (`map`/`player`/`hero`/
  `items`) and the repo's own match JSON, and falls back to a compact key dump
  for anything else, so it never crashes on an unexpected schema.
- Read-only: it only performs a GET; it does not write to the game or the repo.

## Using the output
Reason over the returned clock/HP/economy to ground live advice. If the endpoint
is unreachable the script exits with a clear error — relay that to the user (the
GSI relay likely isn't running) instead of guessing the game state. For deeper
tactics, pair this with the `morphling-knowledge` skill.
