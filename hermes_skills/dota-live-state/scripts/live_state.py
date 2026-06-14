#!/usr/bin/env python3
"""Fetch and summarize the current Dota 2 game state for the Hermes agent.

Helper script behind the `dota-live-state` skill. The agent invokes it via the
terminal toolset:

    python scripts/live_state.py

It GETs the live-state endpoint (default http://localhost:53000/latest) and
prints a compact, token-cheap summary the agent can reason over. Uses only the
Python stdlib (urllib) so it has no third-party dependencies.

The endpoint's JSON schema is not pinned by the repo, so the summarizer is
tolerant: it understands Dota 2 Game State Integration (GSI) shapes
(map/player/hero/items) and the repo's own match JSON, and degrades to a
compact key dump for anything else.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:53000/latest"


def fetch(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def fmt_clock(seconds) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    sign = "-" if s < 0 else ""
    s = abs(s)
    return f"{sign}{s // 60}:{s % 60:02d}"


def summarize_gsi(data: dict):
    """Summarize a Dota 2 GSI-style payload. Returns a list of lines or None."""
    if not isinstance(data, dict):
        return None
    m = data.get("map")
    p = data.get("player")
    h = data.get("hero")
    if not any(isinstance(x, dict) for x in (m, p, h)):
        return None

    lines = []
    if isinstance(m, dict):
        clock = m.get("clock_time", m.get("game_time"))
        if clock is not None:
            lines.append(f"Clock: {fmt_clock(clock)}  (state: {m.get('game_state', '?')})")
        rs, ds = m.get("radiant_score"), m.get("dire_score")
        if rs is not None or ds is not None:
            lines.append(f"Score: Radiant {rs} - Dire {ds}")
        if m.get("ward_purchase_cooldown") is not None:
            lines.append(f"Ward purchase CD: {m.get('ward_purchase_cooldown')}s")

    if isinstance(h, dict):
        hp = h.get("health_percent")
        mp = h.get("mana_percent")
        alive = h.get("alive")
        state = "dead" if alive is False else "alive"
        bits = [f"lvl {h.get('level', '?')}", state]
        if hp is not None:
            bits.append(f"HP {hp}%")
        if mp is not None:
            bits.append(f"mana {mp}%")
        if h.get("respawn_seconds"):
            bits.append(f"respawn {h['respawn_seconds']}s")
        lines.append("Hero: " + ", ".join(str(b) for b in bits))
        if h.get("name"):
            lines.append(f"Playing: {h['name']}")

    if isinstance(p, dict):
        eco = []
        for label, key in (("gold", "gold"), ("net worth", "net_worth"),
                           ("GPM", "gpm"), ("XPM", "xpm"),
                           ("LH", "last_hits"), ("DN", "denies")):
            if p.get(key) is not None:
                eco.append(f"{label} {p[key]}")
        kda = (p.get("kills"), p.get("deaths"), p.get("assists"))
        if any(v is not None for v in kda):
            eco.insert(0, "KDA {}/{}/{}".format(*[v if v is not None else "?" for v in kda]))
        if eco:
            lines.append("Economy: " + ", ".join(eco))

    items = data.get("items")
    if isinstance(items, dict):
        slots = []
        for slot, item in items.items():
            if isinstance(item, dict):
                nm = item.get("name", "")
                if nm and nm != "empty":
                    slots.append(nm.replace("item_", ""))
        if slots:
            lines.append("Items: " + ", ".join(slots))

    return lines or None


def summarize_match(data: dict):
    """Summarize the repo's own match-JSON shape (data/demo_match.json)."""
    if not isinstance(data, dict) or "player" not in data:
        return None
    pl = data.get("player", {})
    if not isinstance(pl, dict) or "hero" not in pl:
        return None
    lines = []
    lines.append(
        f"Match {data.get('match_id', '?')} | patch {data.get('patch', '?')} "
        f"| {data.get('outcome', '?')} | {data.get('duration_min', '?')} min"
    )
    kda = pl.get("kda")
    kda_s = "/".join(str(x) for x in kda) if isinstance(kda, list) else "?"
    lines.append(
        f"{pl.get('hero')} ({pl.get('role')}): KDA {kda_s}, "
        f"GPM {pl.get('gpm')}, XPM {pl.get('xpm')}, "
        f"LH@10 {pl.get('last_hits_at_10')}, net worth {pl.get('net_worth_final')}"
    )
    lp = data.get("lane_phase", {})
    if isinstance(lp, dict) and lp:
        lines.append(
            f"Lane: enemy mid {lp.get('enemy_mid', '?')}, "
            f"deaths 0-10 {lp.get('deaths_0_10min', '?')}, "
            f"LH@5 {lp.get('last_hits_at_5min', '?')}"
        )
    if data.get("enemy_lineup"):
        lines.append("Enemy: " + ", ".join(data["enemy_lineup"]))
    return lines


def compact_dump(data, limit: int = 1200):
    blob = json.dumps(data, indent=2)
    if len(blob) > limit:
        blob = blob[:limit] + "\n... (truncated)"
    return ["Unrecognized schema — compact dump:", blob]


def main():
    ap = argparse.ArgumentParser(description="Summarize live Dota 2 game state.")
    ap.add_argument("--url", default=os.environ.get("DOTA_GSI_URL", DEFAULT_URL),
                    help=f"Live-state endpoint (default {DEFAULT_URL}, or $DOTA_GSI_URL).")
    ap.add_argument("--timeout", type=float, default=5.0, help="Request timeout seconds.")
    ap.add_argument("--json", action="store_true", help="Print raw JSON instead of a summary.")
    args = ap.parse_args()

    try:
        data = fetch(args.url, args.timeout)
    except urllib.error.URLError as e:
        sys.exit(
            f"ERROR: could not reach live-state endpoint {args.url}: {e.reason}\n"
            "Is the Dota GSI relay running? Set DOTA_GSI_URL to override the address."
        )
    except (ValueError, urllib.error.HTTPError) as e:
        sys.exit(f"ERROR: bad response from {args.url}: {e}")

    if args.json:
        print(json.dumps(data, indent=2))
        return

    lines = summarize_gsi(data) or summarize_match(data) or compact_dump(data)
    print(f"# Dota live state  (source: {args.url})")
    for ln in lines:
        print(ln)


if __name__ == "__main__":
    main()
