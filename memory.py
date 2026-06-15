"""ReplaySense episodic memory — the "gets better the more you play" layer.

Every coaching session is persisted to ./episodic_memory/<match_id>.json. On the
next match we load a short summary of recent sessions and feed it back into the
coach prompt, so advice is grounded in the player's actual trajectory:

    "Last 3 sessions you repeatedly overextended at ~7min; last-hits at 10
     averaged 32, target 50+."

Pure local JSON files — no DB, no cloud. Survives across runs and sessions.
"""

import json
import time
from pathlib import Path

try:
    import openshell_sandbox as _sandbox
except Exception:
    _sandbox = None

REPO_ROOT = Path(__file__).parent
EPISODIC_DIR = REPO_ROOT / "episodic_memory"

# Laning benchmarks used to phrase the "vs target" deltas.
TARGET_LAST_HITS_AT_10 = 50
TARGET_EARLY_DEATHS = 1


# ============================================================================
# DERIVATION HELPERS (so agent.py stays lean)
# ============================================================================
def derive_key_mistakes(match: dict) -> list:
    """Pull concrete mistakes from the structured match data."""
    mistakes = []
    for d in match.get("deaths", []):
        ctx = (d.get("context") or "").strip()
        if ctx:
            t = d.get("time_s")
            when = f" (~{int(t) // 60}min)" if isinstance(t, (int, float)) else ""
            mistakes.append(f"{ctx}{when}")
    lh10 = match.get("lane_phase", {}).get("last_hits_at_10min")
    if isinstance(lh10, (int, float)) and lh10 < TARGET_LAST_HITS_AT_10:
        mistakes.append(f"last-hits at 10 only {lh10} (target {TARGET_LAST_HITS_AT_10}+)")
    return mistakes


def extract_growth_area(coaching_markdown: str) -> str:
    """Pull the one-line growth area out of the coach's 'Coach Memory Note'."""
    if not coaching_markdown or "Coach Memory Note" not in coaching_markdown:
        return ""
    tail = coaching_markdown.split("Coach Memory Note", 1)[-1]
    for line in tail.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _match_stats(match: dict) -> dict:
    p = match.get("player", {})
    lp = match.get("lane_phase", {})
    return {
        "last_hits_at_10": lp.get("last_hits_at_10min"),
        "early_deaths": lp.get("deaths_0_10min"),
        "gpm": p.get("gpm"),
        "xpm": p.get("xpm"),
        "net_worth_final": p.get("net_worth_final"),
    }


# ============================================================================
# STORE / LOAD
# ============================================================================
def store_session(match: dict, coaching_markdown: str,
                  key_mistakes: list = None, growth_area: str = None) -> Path:
    """Persist one coaching session as ./episodic_memory/<match_id>.json."""
    EPISODIC_DIR.mkdir(exist_ok=True)
    p = match.get("player", {})
    match_id = str(match.get("match_id", f"unknown_{int(time.time())}"))

    record = {
        "match_id": match_id,
        "timestamp": time.time(),
        "hero": p.get("hero", "morphling"),
        "role": p.get("role", "mid"),
        "outcome": match.get("outcome", "unknown"),
        "stats": _match_stats(match),
        "key_mistakes": key_mistakes if key_mistakes is not None else derive_key_mistakes(match),
        "growth_area": growth_area if growth_area is not None else extract_growth_area(coaching_markdown),
        "full_coaching_markdown": coaching_markdown,
    }
    path = EPISODIC_DIR / f"{match_id}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    # Audit the memory write through OpenShell
    if _sandbox is not None:
        try:
            _sandbox.log_memory_write(match_id, str(path))
        except Exception:
            pass
    return path


def _load_all() -> list:
    if not EPISODIC_DIR.exists():
        return []
    out = []
    for f in EPISODIC_DIR.glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def get_history(hero: str = None, role: str = None, limit: int = 5) -> list:
    """Return recent prior sessions, newest first, optionally filtered."""
    records = _load_all()
    if hero:
        records = [r for r in records if r.get("hero", "").lower() == hero.lower()]
    if role:
        records = [r for r in records if r.get("role", "").lower() == role.lower()]
    records.sort(key=lambda r: r.get("timestamp", 0), reverse=True)
    return records[:limit]


# ============================================================================
# MEMORY CONTEXT — the short string the coach actually reads
# ============================================================================
def _avg(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    return sum(nums) / len(nums) if nums else None


def build_memory_context(hero: str = "morphling", role: str = "mid",
                         limit: int = 3, exclude_match_id: str = None) -> str:
    """Summarize the last N sessions into a short, grounded coaching prime."""
    history = get_history(hero, role, limit=limit + 1)
    if exclude_match_id:
        history = [r for r in history if str(r.get("match_id")) != str(exclude_match_id)]
    history = history[:limit]
    if not history:
        return ("No prior logged sessions for this hero/role — this is the first "
                "game on record. Establish a baseline.")

    n = len(history)
    wins = sum(1 for r in history if r.get("outcome") == "win")
    losses = sum(1 for r in history if r.get("outcome") == "loss")

    lh = _avg([r.get("stats", {}).get("last_hits_at_10") for r in history])
    deaths = _avg([r.get("stats", {}).get("early_deaths") for r in history])
    gpm = _avg([r.get("stats", {}).get("gpm") for r in history])

    # Recurring mistake themes across sessions.
    theme_counts = {}
    for r in history:
        for m in r.get("key_mistakes", []):
            key = _theme_key(m)
            theme_counts[key] = theme_counts.get(key, 0) + 1
    recurring = sorted(theme_counts.items(), key=lambda kv: kv[1], reverse=True)

    parts = [f"Across your last {n} {hero} {role} session{'s' if n != 1 else ''} "
             f"({wins}W-{losses}L):"]
    if lh is not None:
        delta = "below" if lh < TARGET_LAST_HITS_AT_10 else "at/above"
        parts.append(f"last-hits@10 averaged {lh:.0f} ({delta} the {TARGET_LAST_HITS_AT_10}+ target);")
    if deaths is not None:
        parts.append(f"early deaths averaged {deaths:.1f};")
    if gpm is not None:
        parts.append(f"GPM averaged {gpm:.0f};")

    top_recurring = [k for k, c in recurring if c >= 2]
    if top_recurring:
        parts.append("recurring theme" + ("s" if len(top_recurring) > 1 else "") +
                     ": " + "; ".join(top_recurring[:2]) + ".")
    elif recurring:
        parts.append(f"most recent issue: {recurring[0][0]}.")

    last_growth = history[0].get("growth_area")
    if last_growth:
        parts.append(f"Last stated growth area: \"{last_growth}\"")

    return " ".join(parts)


_THEME_WORDS = [
    "overextend", "ward", "bkb", "manta", "last-hit", "last hit", "rune",
    "rotation", "tower", "rosh", "positioning", "trade", "farm", "escape",
]


def _theme_key(mistake: str) -> str:
    """Collapse a specific mistake into a coarse theme for frequency counting."""
    m = mistake.lower()
    for w in _THEME_WORDS:
        if w in m:
            return f"{w}-related issues"
    # Fall back to the first few words of the raw mistake.
    return " ".join(mistake.split()[:4])


if __name__ == "__main__":
    print("Episodic memory store:", EPISODIC_DIR)
    print(build_memory_context())
    for r in get_history():
        print(f"  - {r['match_id']} {r['outcome']} lh@10={r['stats'].get('last_hits_at_10')}")
