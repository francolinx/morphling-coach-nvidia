"""live_loop.py — ReplaySense live-game coaching layer (Tab B).

The always-on coaching loop that drives the "Live Game" tab. It is deliberately
decoupled from the UI so it can be unit-tested and so the dashboard stays thin.

Two data sources, one interface:

    get_game_state(mode="live",   ...)  -> poll the GSI server at /latest
    get_game_state(mode="cached", step) -> step through a timeline synthesized
                                           from data/demo_match.json

This guarantees the Live Game tab demos cleanly even if the Dota bot match or
the GSI server isn't firing during judging.

Coaching is produced by agent.coach_match() — we never reimplement it; we only
wrap it with timing + exception handling so a model hiccup degrades to the
cached fallback instead of crashing the console.
"""

import json
import os
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).parent
DATA_DIR = REPO_ROOT / "data"
DEMO_MATCH_PATH = DATA_DIR / "demo_match.json"
CACHED_RESPONSE_PATH = DATA_DIR / "cached_response.md"

# GSI server (gsi/gsi_server.py) — overridable, never hardcode in the UI.
GSI_URL = os.environ.get("REPLAYSENSE_GSI_URL", "http://localhost:53000/latest")

# ============================================================================
# GAME PHASE
# ============================================================================
# Laning phases the corpus is organized around (0-3 / 3-6 / 6-10 min).
PHASE_BOUNDS = [
    (180, "0–3 min · Opening Lane"),
    (360, "3–6 min · Trade Windows"),
    (600, "6–10 min · Lane → Mid Transition"),
]


def game_phase(clock_s: int) -> str:
    """Derive the coaching phase label from the GSI clock (seconds)."""
    for bound, label in PHASE_BOUNDS:
        if clock_s < bound:
            return label
    return ">10 min · Mid Game"


def phase_index(clock_s: int) -> int:
    """0-based index of the current phase (used to detect phase boundaries)."""
    for i, (bound, _) in enumerate(PHASE_BOUNDS):
        if clock_s < bound:
            return i
    return len(PHASE_BOUNDS)


def fmt_clock(clock_s: int) -> str:
    """Seconds -> M:SS game clock string."""
    clock_s = int(clock_s)
    sign = "-" if clock_s < 0 else ""
    clock_s = abs(clock_s)
    return f"{sign}{clock_s // 60}:{clock_s % 60:02d}"


# ============================================================================
# LIVE SOURCE — GSI server
# ============================================================================
def _extract_clock(gsi: dict) -> int:
    """Pull the game clock out of a Dota GSI payload, defensively.

    Different GSI server implementations nest this differently; we try the
    common locations and fall back to 0.
    """
    if not isinstance(gsi, dict):
        return 0
    m = gsi.get("map") or gsi.get("Map") or {}
    for key in ("clock_time", "game_time", "clock_s", "clock"):
        if isinstance(m, dict) and isinstance(m.get(key), (int, float)):
            return int(m[key])
    for key in ("clock_s", "clock_time", "game_time", "clock"):
        if isinstance(gsi.get(key), (int, float)):
            return int(gsi[key])
    return 0


def poll_gsi(url: str = None, timeout: float = 2.0) -> dict:
    """Fetch the latest live game state from the GSI server.

    Returns a normalized envelope; never raises — connection problems are
    reported via ok=False so the UI can show "waiting for bot match".
    """
    url = url or GSI_URL
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        gsi = r.json()
    except Exception as e:  # noqa: BLE001 — UI wants the message, not a crash
        return {"ok": False, "raw": None, "clock_s": 0, "note": f"GSI unreachable: {e}"}

    clock_s = _extract_clock(gsi)
    return {
        "ok": True,
        "raw": gsi,
        "clock_s": clock_s,
        "note": "live GSI",
        # The agent consumes a match-like dict; we pass the live state through
        # plus the demo match shape so corpus retrieval still has an enemy mid.
        "match_like": _live_match_like(gsi, clock_s),
    }


def _live_match_like(gsi: dict, clock_s: int) -> dict:
    """Map a live GSI snapshot onto the match dict shape coach_match expects."""
    base = _load_demo_match()
    hero = (gsi.get("hero") or {}) if isinstance(gsi, dict) else {}
    player = (gsi.get("player") or {}) if isinstance(gsi, dict) else {}
    return {
        "match_id": "LIVE",
        "patch": base.get("patch", "7.41b"),
        "live_clock_s": clock_s,
        "player": {
            "hero": (hero.get("name") or base.get("player", {}).get("hero", "morphling")),
            "role": "mid",
            "gpm": player.get("gpm"),
            "xpm": player.get("xpm"),
            "last_hits": player.get("last_hits"),
            "kda": [player.get("kills"), player.get("deaths"), player.get("assists")],
        },
        "lane_phase": base.get("lane_phase", {}),
        "live_gsi": gsi,
    }


# ============================================================================
# CACHED SOURCE — timeline synthesized from demo_match.json
# ============================================================================
def _load_demo_match() -> dict:
    try:
        return json.loads(DEMO_MATCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_cached_timeline(match: dict = None) -> list:
    """Build an ordered list of game-state snapshots from a finished match.

    Each snapshot is the match dict "as of" a given clock: deaths that have
    happened so far, an interpolated last-hit count, and the live clock. The
    Live Game tab steps through these to simulate an always-on feed without a
    running bot match.
    """
    match = match or _load_demo_match()
    deaths = sorted(match.get("deaths", []), key=lambda d: d.get("time_s", 0))
    lane = match.get("lane_phase", {})
    lh5 = lane.get("last_hits_at_5min", 17)
    lh10 = lane.get("last_hits_at_10min", 31)

    # Sample the laning window every 60 game-seconds, and snap to each death
    # so a coaching turn lands right when something happened.
    sample_clocks = list(range(60, 661, 60))
    death_clocks = [d.get("time_s", 0) for d in deaths if d.get("time_s", 0) <= 660]
    clocks = sorted(set(sample_clocks + death_clocks))

    timeline = []
    for clock_s in clocks:
        # Crude but readable CS interpolation across the lane.
        if clock_s <= 300:
            lh = round(lh5 * clock_s / 300)
        elif clock_s <= 600:
            lh = round(lh5 + (lh10 - lh5) * (clock_s - 300) / 300)
        else:
            lh = round(lh10 + (clock_s - 600) / 60 * 8)
        deaths_so_far = [d for d in deaths if d.get("time_s", 0) <= clock_s]

        snap = dict(match)
        snap["match_id"] = f"{match.get('match_id', 'demo')}@{fmt_clock(clock_s)}"
        snap["live_clock_s"] = clock_s
        snap["deaths"] = deaths_so_far
        snap["lane_phase"] = {**lane, "last_hits_so_far": lh}
        # Whether this snapshot lands exactly on a death (UI marker).
        snap["_event"] = next(
            (d for d in deaths if d.get("time_s", 0) == clock_s), None
        )
        timeline.append(
            {
                "ok": True,
                "clock_s": clock_s,
                "phase": game_phase(clock_s),
                "phase_index": phase_index(clock_s),
                "last_hits": lh,
                "deaths_so_far": len(deaths_so_far),
                "event": snap["_event"],
                "match_like": snap,
                "note": "cached timeline",
            }
        )
    return timeline


# ============================================================================
# UNIFIED INTERFACE
# ============================================================================
def get_game_state(mode: str = "cached", step: int = 0, match: dict = None,
                   url: str = None, timeline: list = None) -> dict:
    """Single entry point for the Live Game tab.

    mode="live"   -> poll the GSI server (returns ok=False if unreachable).
    mode="cached" -> return snapshot `step` of a synthesized timeline.

    Returned dict always has: ok, clock_s, phase, match_like, note.
    """
    if mode == "live":
        state = poll_gsi(url=url)
        state["phase"] = game_phase(state.get("clock_s", 0))
        state["phase_index"] = phase_index(state.get("clock_s", 0))
        state.setdefault("match_like", _live_match_like(state.get("raw") or {}, state.get("clock_s", 0)))
        state["mode"] = "live"
        return state

    timeline = timeline if timeline is not None else build_cached_timeline(match)
    if not timeline:
        return {"ok": False, "clock_s": 0, "phase": game_phase(0),
                "match_like": _load_demo_match(), "note": "no timeline", "mode": "cached"}
    step = max(0, min(step, len(timeline) - 1))
    state = dict(timeline[step])
    state["mode"] = "cached"
    state["step"] = step
    state["total_steps"] = len(timeline)
    return state


# ============================================================================
# COACHING TURN — wraps agent.coach_match with timing + fallback
# ============================================================================
def load_cached_markdown() -> str:
    try:
        return CACHED_RESPONSE_PATH.read_text(encoding="utf-8")
    except Exception:
        return "_Cached coaching response not found (data/cached_response.md)._"


def run_coach_turn(match_like: dict, force_cached: bool = False) -> dict:
    """Run one coaching turn. Never raises.

    Returns: {markdown, latency_s, source ("model"|"cached"), error}
    """
    if force_cached:
        return {"markdown": load_cached_markdown(), "latency_s": 0.0,
                "source": "cached", "error": None}

    # Import here so the UI can still load even if agent.py is mid-edit (Tab A).
    try:
        from agent import coach_match
    except Exception as e:  # noqa: BLE001
        return {"markdown": load_cached_markdown(), "latency_s": 0.0,
                "source": "cached", "error": f"agent import failed: {e}"}

    start = time.time()
    try:
        md = coach_match(match_like)
        latency = time.time() - start
        if not md or not str(md).strip():
            raise ValueError("empty response from model")
        return {"markdown": md, "latency_s": latency, "source": "model", "error": None}
    except Exception as e:  # noqa: BLE001 — degrade to cached, keep the demo alive
        return {"markdown": load_cached_markdown(), "latency_s": time.time() - start,
                "source": "cached", "error": str(e)}


if __name__ == "__main__":
    # Smoke test the abstraction without a UI or a model.
    tl = build_cached_timeline()
    print(f"cached timeline: {len(tl)} snapshots")
    for s in tl:
        ev = f"  <- death by {s['event']['killed_by']}" if s["event"] else ""
        print(f"  {fmt_clock(s['clock_s']):>6}  {s['phase']:<32} LH={s['last_hits']:>2} "
              f"deaths={s['deaths_so_far']}{ev}")
    print("\nlive poll:", {k: v for k, v in poll_gsi().items() if k != "match_like"})
