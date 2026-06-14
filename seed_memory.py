"""Pre-populate episodic memory for the live demo.

Batches the first N matches from data/my_morphling_matches.json through
agent.coach_match() so the dashboard's Coach Memory panel has real history to
show before anyone touches it.

The source file is a list of OpenDota match *summaries* — real outcome, K/D/A,
duration and hero, but NOT the rich per-minute fields (lane_phase, deaths[],
item_timings, enemy_lineup) that demo_match.json has. We adapt only the fields
that are genuinely present; we never fabricate stats.

Idempotent: any match_id already in episodic_memory/ is skipped, so you can
re-run it safely (e.g. after adding more matches).

Usage (PowerShell):
    python seed_memory.py
    python seed_memory.py --count 10
    python seed_memory.py --dry-run        # adapt + report, no model calls
"""

import argparse
import json
from pathlib import Path

import agent
import memory

REPO_ROOT = Path(__file__).parent
MATCHES_PATH = REPO_ROOT / "data" / "my_morphling_matches.json"

# OpenDota hero_id -> name (only the heroes we expect here).
HERO_NAMES = {10: "morphling"}


def load_match_summaries(path: Path) -> list:
    """Load the match list, tolerating a truncated/missing closing bracket."""
    raw = path.read_text(encoding="utf-8").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Recover every complete top-level object from the array body.
        dec = json.JSONDecoder()
        i = raw.index("[") + 1
        recs = []
        while i < len(raw):
            while i < len(raw) and raw[i] in " ,\n\r\t":
                i += 1
            if i >= len(raw) or raw[i] == "]":
                break
            try:
                obj, end = dec.raw_decode(raw, i)
            except json.JSONDecodeError:
                break
            recs.append(obj)
            i = end
        return recs


def adapt(summary: dict) -> dict:
    """Map an OpenDota summary into the match shape coach_match expects.

    Only real, derivable fields are populated. Missing detail (last hits, death
    contexts, enemy draft) is left out rather than invented.
    """
    is_radiant = summary.get("player_slot", 0) < 128
    radiant_win = bool(summary.get("radiant_win"))
    outcome = "win" if is_radiant == radiant_win else "loss"
    duration = summary.get("duration") or 0
    hero = HERO_NAMES.get(summary.get("hero_id"), "morphling")

    return {
        "match_id": str(summary.get("match_id")),
        "patch": "7.41b",
        "outcome": outcome,
        "duration_min": round(duration / 60) if duration else None,
        "start_time": summary.get("start_time"),
        "source": "opendota_summary",
        "player": {
            "hero": hero,
            "role": "mid",
            "kda": [summary.get("kills", 0), summary.get("deaths", 0), summary.get("assists", 0)],
        },
    }


def already_seeded(match_id: str) -> bool:
    return (memory.EPISODIC_DIR / f"{match_id}.json").exists()


def main():
    parser = argparse.ArgumentParser(description="Seed episodic memory for the demo")
    parser.add_argument("--count", type=int, default=8,
                        help="how many matches to seed (default 8)")
    parser.add_argument("--dry-run", action="store_true",
                        help="adapt + report only; do not call the model or write memory")
    args = parser.parse_args()

    summaries = load_match_summaries(MATCHES_PATH)
    batch = summaries[:args.count]
    print(f"Loaded {len(summaries)} match summaries; seeding first {len(batch)} "
          f"(model: {agent.MODEL_NAME} @ {agent.MODEL_URL})\n")

    rows = []
    for idx, summ in enumerate(batch, 1):
        match = adapt(summ)
        mid = match["match_id"]
        k, d, a = match["player"]["kda"]
        kda = f"{k}/{d}/{a}"

        if already_seeded(mid):
            rows.append((idx, mid, match["outcome"], kda, "skipped (exists)", ""))
            print(f"[{idx}/{len(batch)}] {mid}  {match['outcome']:4}  {kda:8}  skipped (already seeded)")
            continue

        if args.dry_run:
            rows.append((idx, mid, match["outcome"], kda, "dry-run", ""))
            print(f"[{idx}/{len(batch)}] {mid}  {match['outcome']:4}  {kda:8}  dry-run (not written)")
            continue

        print(f"[{idx}/{len(batch)}] {mid}  {match['outcome']:4}  {kda:8}  coaching... ", end="", flush=True)
        try:
            md = agent.coach_match(match)
            growth = memory.extract_growth_area(md) or "(no memory note returned)"
            rows.append((idx, mid, match["outcome"], kda, "seeded", growth))
            print("done")
        except Exception as e:
            rows.append((idx, mid, match["outcome"], kda, f"ERROR: {type(e).__name__}", str(e)[:60]))
            print(f"FAILED ({type(e).__name__}: {e})")

    _print_table(rows)

    # Show the summary the dashboard's Coach Memory panel will read.
    print("\nResulting Coach Memory context (morphling / mid):")
    print("  " + memory.build_memory_context("morphling", "mid", limit=args.count))


def _print_table(rows):
    print("\n" + "=" * 78)
    print(f"{'#':<3} {'match_id':<12} {'result':<6} {'K/D/A':<9} {'status':<18} growth area")
    print("-" * 78)
    for idx, mid, outcome, kda, status, growth in rows:
        print(f"{idx:<3} {mid:<12} {outcome:<6} {kda:<9} {status:<18} {growth[:30]}")
    seeded = sum(1 for r in rows if r[4] == "seeded")
    skipped = sum(1 for r in rows if r[4].startswith("skipped"))
    errors = sum(1 for r in rows if r[4].startswith("ERROR"))
    print("=" * 78)
    print(f"seeded={seeded}  skipped={skipped}  errors={errors}  "
          f"(memory dir: {memory.EPISODIC_DIR})")


if __name__ == "__main__":
    main()
