# Morphling Hermes Skills — Setup

Two local Hermes skills that expose ReplaySense capabilities natively to the
agent running under NVIDIA NemoClaw on the GB10:

| Skill | Slash command | What it does |
|-------|---------------|--------------|
| `morphling-knowledge` | `/morphling-knowledge` | RAG over the local Morphling/Dota corpus (wraps `agent.load_corpus`). |
| `dota-live-state` | `/dota-live-state` | GETs the live GSI relay (`:53000/latest`) and summarizes current game state. |

Both are knowledge documents (`SKILL.md`) plus a stdlib-friendly helper script
in `scripts/`. The agent reads the SKILL.md and runs the script via its terminal
toolset.

```
hermes_skills/
├── morphling-knowledge/
│   ├── SKILL.md
│   └── scripts/retrieve.py
└── dota-live-state/
    ├── SKILL.md
    └── scripts/live_state.py
```

## Prerequisites
- The ReplaySense repo checkout (this repo) is on the GB10. The skills locate it
  via `$REPLAYSENSE_REPO`, or by walking up from the script's own path if the
  skills stay inside the repo.
- For `dota-live-state`: the Dota GSI relay is serving `http://localhost:53000/latest`.
- `python3` on PATH. `morphling-knowledge` reuses `agent.load_corpus`; if
  `agent.py`'s deps (`requests`) aren't installed in the Hermes runtime it
  transparently falls back to an equivalent corpus loader, so no extra install
  is required.

## Install the local skills

Hermes treats `~/.hermes/skills/` as the single source of truth and registers
every skill dir there as a slash command. Copy these two dirs into a category
folder under it:

```bash
mkdir -p ~/.hermes/skills/coaching
cp -r hermes_skills/morphling-knowledge ~/.hermes/skills/coaching/
cp -r hermes_skills/dota-live-state     ~/.hermes/skills/coaching/

# Point the knowledge skill at this repo checkout (adjust the path):
export REPLAYSENSE_REPO=/home/user/morphling-coach-nvidia
# Optional: override the live-state endpoint
export DOTA_GSI_URL=http://localhost:53000/latest
```

Verify Hermes sees them, then preload them into a chat session:

```bash
hermes skills list
hermes chat -s morphling-knowledge,dota-live-state
```

`hermes chat --skills <name>` is the long form; `-s` is repeatable or
comma-separated. Once loaded, the agent can call `/morphling-knowledge ...` and
`/dota-live-state`, or invoke them in natural conversation.

### Under NemoClaw (gateway auto-pickup)
The NemoClaw "NemoClaw for Hermes Agent" blueprint auto-loads skills the gateway
finds in its `agents/hermes/skills/` folder. If you're running the blueprint,
drop the two skill dirs there instead and restart the gateway:

```bash
cp -r hermes_skills/morphling-knowledge agents/hermes/skills/
cp -r hermes_skills/dota-live-state     agents/hermes/skills/
```

> ⚠️ **Verify before relying on this:** the `agents/hermes/skills/` auto-pickup
> path comes from the NemoClaw blueprint description on build.nvidia.com, which I
> could not cross-check against `docs.nvidia.com/nemoclaw` (it returned 403 to
> the fetcher). Confirm the exact folder in your blueprint checkout. The
> `~/.hermes/skills/` install above is the documented, vendor-neutral path and
> should work regardless.

## Smoke test (no Hermes needed)
The scripts run standalone, which is the fastest way to confirm they work:

```bash
python hermes_skills/morphling-knowledge/scripts/retrieve.py "manta timing vs lina" --max 3
python hermes_skills/dota-live-state/scripts/live_state.py      # needs the GSI relay up
```

## What's verified vs. assumed
**Verified** against the NousResearch/hermes-agent docs:
- Skill = a directory with a required `SKILL.md` (YAML frontmatter: `name`,
  `description`, `version`; optional `platforms`, `metadata.hermes.{tags,
  category, requires_toolsets, config, ...}`) plus optional `scripts/`,
  `references/`, `templates/`, `assets/`.
- Skills live in `~/.hermes/skills/`, auto-register as slash commands, and load
  with `hermes chat -s/--skills`. (`hermes skills install/list/inspect/...`.)

**Assumed / flagged:**
- The exact convention by which Hermes invokes files in `scripts/` is not
  documented. We follow the natural model: SKILL.md instructs the agent to run
  the script through its terminal toolset. If your Hermes build expects a
  different handler entrypoint, the SKILL.md "How to run" block is the only thing
  that needs adjusting — the scripts are plain CLI tools.
- The `metadata.hermes.config` → environment-variable wiring isn't pinned down,
  so each script's source of truth is its env var (`REPLAYSENSE_REPO`,
  `DOTA_GSI_URL`) with auto-detection; the `config` blocks are declared for the
  Hermes setup UI but you can ignore them and just export the env vars.
- The live-state JSON schema at `:53000/latest` isn't pinned by the repo. The
  summarizer handles Dota 2 GSI shapes and the repo's match JSON, and degrades to
  a compact key dump otherwise — so it won't crash on a surprise schema, but
  tune `summarize_gsi()` once you see the real payload.

## Note on the corpus wrapper
The task brief referenced `rag.py`/`memory.py`, which don't exist in this repo —
the retrieval logic lives in `agent.py` (`load_corpus`, `retrieve`).
`morphling-knowledge` imports `load_corpus` from `agent.py` **read-only** and adds
its own free-text keyword ranking (the existing `agent.retrieve` is driven by
live match-state, not a query string). No repo files were modified.
