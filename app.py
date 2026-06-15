"""ReplaySense Coaching Console — local-first AI coach for competitive Dota 2.

Tab B: the face + live layer.
  - Match Review tab : load demo match -> coach_match() -> 6-section report.
  - Live Game tab    : always-on coaching loop over live GSI or a cached timeline.
  - Phase timeline   : visual of where the agent coached and where you died.
  - Coach Memory     : episodic memory the agent accumulates across sessions.

Everything runs on-device. No cloud SDKs, no browser storage, requests + Streamlit.
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path

import requests
import streamlit as st

import live_loop
from live_loop import (
    build_cached_timeline,
    fmt_clock,
    game_phase,
    get_game_state,
    load_cached_markdown,
    run_coach_turn,
)

# agent.py is owned by the engine session (Tab A) — import defensively so the
# console still loads if that module is mid-edit.
try:
    from agent import MODEL_NAME, MODEL_URL
except Exception:  # noqa: BLE001
    MODEL_NAME = os.environ.get("REPLAYSENSE_MODEL_NAME", "qwen3:8b")
    MODEL_URL = os.environ.get("REPLAYSENSE_MODEL_URL", "http://localhost:11434/api/chat")

try:
    from openshell_sandbox import get_sandbox_status
except Exception:
    def get_sandbox_status():
        return {"sandboxed": False, "badge": "⚠️ OpenShell not installed",
                "network_egress": "unrestricted", "data_stays_local": True,
                "openshell_available": False}

API_KEY = os.environ.get("REPLAYSENSE_API_KEY", "")
GSI_URL = live_loop.GSI_URL

REPO_ROOT = Path(__file__).parent
DEMO_MATCH_PATH = REPO_ROOT / "data" / "demo_match.json"
EPISODIC_DIR = REPO_ROOT / "episodic_memory"
LEGACY_MEMORY = REPO_ROOT / "coach_memory.json"

st.set_page_config(
    page_title="ReplaySense — Local AI Coach",
    page_icon="🎮",
    layout="wide",
)

# ============================================================================
# STYLE
# ============================================================================
st.markdown(
    """
    <style>
      .rs-badge {
        display:inline-block; padding:6px 14px; border-radius:8px;
        background:linear-gradient(90deg,#0b3d0b,#145214); color:#9dffb0;
        font-weight:700; letter-spacing:.5px; border:1px solid #1f7a1f;
        font-size:0.85rem;
      }
      .rs-feed {
        border-left:3px solid #76b900; padding:8px 14px; margin:10px 0;
        background:rgba(118,185,0,0.06); border-radius:4px;
      }
      .rs-feed .meta { color:#9aa0a6; font-size:0.8rem; margin-bottom:4px; }
      .rs-track { position:relative; height:46px; border-radius:6px; overflow:hidden;
                  background:#1b1d22; margin:6px 0 26px 0; }
      .rs-seg { position:absolute; top:0; height:100%; opacity:0.5;
                display:flex; align-items:center; justify-content:center;
                font-size:0.72rem; color:#e8e8e8; }
      .rs-marker { position:absolute; top:-2px; transform:translateX(-50%); }
      .rs-pin { font-size:1.1rem; }
      .rs-lbl { position:absolute; top:50px; transform:translateX(-50%);
                font-size:0.68rem; color:#9aa0a6; white-space:nowrap; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ============================================================================
# SESSION STATE
# ============================================================================
st.session_state.setdefault("live_feed", [])      # list of coaching turns
st.session_state.setdefault("cached_step", -1)     # last played snapshot index
st.session_state.setdefault("last_turn_clock", None)
st.session_state.setdefault("review_result", None)


# ============================================================================
# HELPERS
# ============================================================================
def check_endpoint(url: str, timeout: float = 1.5) -> dict:
    """Best-effort reachability check for the model endpoint.

    A POST endpoint may answer GET with 4xx/405 — that still means the host is
    up, which is what we want to convey.
    """
    try:
        r = requests.get(url, timeout=timeout)
        return {"up": True, "detail": f"HTTP {r.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"up": False, "detail": "connection refused"}
    except requests.exceptions.Timeout:
        return {"up": False, "detail": "timeout"}
    except Exception as e:  # noqa: BLE001 — some hosts reject GET oddly but are up
        return {"up": True, "detail": type(e).__name__}


def load_demo_match() -> dict:
    try:
        return json.loads(DEMO_MATCH_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def append_turn(state: dict, result: dict):
    """Record one coaching turn in the live feed."""
    st.session_state.live_feed.append(
        {
            "clock_s": state.get("clock_s", 0),
            "phase": state.get("phase", ""),
            "markdown": result["markdown"],
            "latency_s": result["latency_s"],
            "source": result["source"],
            "error": result.get("error"),
            "wall": datetime.now().strftime("%H:%M:%S"),
        }
    )
    st.session_state.last_turn_clock = state.get("clock_s", 0)


def render_phase_timeline(turn_clocks, death_events, window_s=660):
    """Horizontal laning-phase timeline (0–11 min) with coaching + death pins.

    Pure HTML/CSS so it has no plotting dependency.
    """
    bands = [
        (0, 180, "0–3 min", "#1f4d2b"),
        (180, 360, "3–6 min", "#264f6b"),
        (360, 600, "6–10 min", "#5a3d6b"),
        (600, window_s, "10 min+", "#4a4a4a"),
    ]
    segs = ""
    for start, end, label, color in bands:
        left = 100 * start / window_s
        width = 100 * (min(end, window_s) - start) / window_s
        segs += (
            f'<div class="rs-seg" style="left:{left:.2f}%;width:{width:.2f}%;'
            f'background:{color}">{label}</div>'
        )
    markers = ""
    for c in turn_clocks:
        if c is None:
            continue
        left = 100 * min(c, window_s) / window_s
        markers += (
            f'<div class="rs-marker" style="left:{left:.2f}%">'
            f'<span class="rs-pin" title="coached @ {fmt_clock(c)}">🟢</span></div>'
        )
    for d in death_events:
        c = d.get("time_s", 0)
        capped = min(c, window_s)
        left = 100 * capped / window_s
        killer = d.get("killed_by", "?")
        off = "" if c <= window_s else f" ({fmt_clock(c)})"
        markers += (
            f'<div class="rs-marker" style="left:{left:.2f}%">'
            f'<span class="rs-pin" title="death by {killer} @ {fmt_clock(c)}">💀</span></div>'
            f'<div class="rs-lbl" style="left:{left:.2f}%">{killer}{off}</div>'
        )
    st.markdown(
        f'<div class="rs-track">{segs}{markers}</div>',
        unsafe_allow_html=True,
    )
    st.caption("🟢 coaching turn fired   ·   💀 death from match data")


def render_coach_memory():
    """Read episodic_memory/*.json (Tab A) and the legacy coach_memory.json."""
    sessions = []
    if EPISODIC_DIR.exists():
        for f in sorted(EPISODIC_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            records = data if isinstance(data, list) else [data]
            for rec in records:
                sessions.append({"file": f.name, "rec": rec})
    if LEGACY_MEMORY.exists():
        try:
            legacy = json.loads(LEGACY_MEMORY.read_text(encoding="utf-8"))
            for mid, rec in legacy.items():
                sessions.append({"file": "coach_memory.json", "rec": {"match_id": mid, **rec}})
        except Exception:
            pass

    if not sessions:
        st.caption(
            "No episodic memory yet. As the agent coaches matches, sessions land "
            "in `./episodic_memory/` and appear here — this is the agent learning "
            "over time."
        )
        return

    st.caption(f"{len(sessions)} remembered session(s) · the agent's growth log")
    for s in sessions[-8:][::-1]:
        rec = s["rec"]
        note = (
            rec.get("growth_area")
            or rec.get("summary")
            or rec.get("note")
            or rec.get("memory_note")
            or ""
        )
        ts = rec.get("timestamp")
        when = ""
        if isinstance(ts, (int, float)):
            when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        elif isinstance(ts, str):
            when = ts
        mid = rec.get("match_id", "—")
        with st.container():
            st.markdown(f"**Match `{mid}`**  ·  {when}  ·  _{s['file']}_")
            if note:
                st.markdown(f"> {note}")
            else:
                st.json(rec)


# ============================================================================
# HEADER
# ============================================================================
left, right = st.columns([3, 2])
with left:
    st.title("🎮 ReplaySense Coaching Console")
    st.caption("Local-first, self-evolving AI coach for competitive Dota 2.")
with right:
    st.markdown(
        '<div style="text-align:right;margin-top:18px">'
        '<span class="rs-badge">🔒 LOCAL ONLY · NO CLOUD · OpenShell Sandboxed</span>'
        "</div>",
        unsafe_allow_html=True,
    )

# ============================================================================
# SIDEBAR
# ============================================================================
with st.sidebar:
    st.header("🖥️ Hardware")
    st.markdown(
        "**Dell Pro Max with GB10**\n\n"
        "- NVIDIA Grace Blackwell\n"
        "- 128 GB unified memory\n"
        "- Nemotron 3 Super 120B\n"
        "- via **Hermes / NemoClaw**\n"
        "- **OpenShell** sandbox"
    )
    st.divider()

    st.header("📡 Endpoint")
    status = check_endpoint(MODEL_URL)
    if status["up"]:
        st.success(f"Model endpoint reachable\n\n`{MODEL_URL}`\n\n{status['detail']}")
    else:
        st.error(f"Model endpoint down ({status['detail']})\n\n`{MODEL_URL}`\n\nFallback recommended.")
    st.caption(f"Model: `{MODEL_NAME}`")
    st.caption(f"API key: {'set ✓' if API_KEY else 'not set (local, none needed)'}")
    gsi_status = check_endpoint(GSI_URL, timeout=1.0)
    st.caption(f"GSI: {'🟢 live' if gsi_status['up'] else '⚪ no bot match'} · `{GSI_URL}`")
    st.divider()

    st.header("🔒 OpenShell Sandbox")
    sandbox = get_sandbox_status()
    if sandbox["sandboxed"]:
        st.success(f"✅ Sandboxed\n\n`{sandbox['sandbox_id']}`")
        st.caption(f"Policy: `{sandbox['policy_name']}`")
    elif sandbox["openshell_available"]:
        st.warning("⚠️ OpenShell installed but not active\n\nRun via `./launch.sh`")
    else:
        st.info("ℹ️ OpenShell not installed\n\nApp still runs 100% local")
    st.caption(f"Network egress: **{sandbox['network_egress']}**")
    st.caption(f"Data stays local: **{'✅ Yes' if sandbox['data_stays_local'] else '❌ No'}**")
    st.divider()

    st.header("🛟 Fallback Mode")
    fallback = st.toggle(
        "Use cached coaching",
        value=not status["up"],
        help="Skip the live model and render data/cached_response.md. "
        "Insurance for judging if the GB10 / Hermes hiccups.",
    )
    if fallback:
        st.warning("Fallback ON — showing pre-computed coaching.")
    else:
        st.info("Fallback OFF — live on-device inference.")

# ============================================================================
# TABS
# ============================================================================
match_data = load_demo_match()
tab_live, tab_review = st.tabs(["🟢 Live Game", "📊 Match Review"])

# ---------------------------------------------------------------------------
# MATCH REVIEW
# ---------------------------------------------------------------------------
with tab_review:
    st.subheader("Match Review")
    if not match_data:
        st.warning("data/demo_match.json not found.")
    else:
        p = match_data.get("player", {})
        lane = match_data.get("lane_phase", {})
        kda = p.get("kda", [0, 0, 0])
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Hero", str(p.get("hero", "—")).title())
        c2.metric("Result", str(match_data.get("outcome", "—")).upper())
        c3.metric("KDA", f"{kda[0]}/{kda[1]}/{kda[2]}")
        c4.metric("GPM / XPM", f"{p.get('gpm','—')} / {p.get('xpm','—')}")
        c5.metric("LH @10", lane.get("last_hits_at_10min", "—"))

        with st.expander("Raw match data", expanded=False):
            st.json(match_data, expanded=False)

        analyze = st.button("▶ Analyze Match", type="primary", use_container_width=True)
        report_area = st.container()

        if analyze:
            with st.spinner(
                "Coaching on cached fallback…" if fallback
                else "Local inference on GB10 (Nemotron via Hermes)…"
            ):
                result = run_coach_turn(match_data, force_cached=fallback)
            st.session_state.review_result = result

        result = st.session_state.review_result
        if result:
            with report_area:
                m1, m2 = st.columns([1, 3])
                if result["source"] == "model":
                    m1.metric("Inference latency", f"{result['latency_s']:.2f} s")
                    m2.success("✓ Generated on-device — no data left the GB10.")
                else:
                    m1.metric("Latency", "cached")
                    if result.get("error"):
                        m2.warning(f"Model unavailable → cached fallback. ({result['error']})")
                    else:
                        m2.info("Cached fallback coaching (Fallback Mode).")
                st.divider()
                st.markdown(result["markdown"])

        st.divider()
        st.subheader("🧭 Laning Phase Timeline")
        render_phase_timeline(
            turn_clocks=[],
            death_events=match_data.get("deaths", []),
        )

# ---------------------------------------------------------------------------
# LIVE GAME
# ---------------------------------------------------------------------------
with tab_live:
    st.subheader("Live Game Coaching")

    ctrl1, ctrl2, ctrl3 = st.columns([2, 2, 3])
    with ctrl1:
        source_mode = st.radio(
            "Game-state source",
            ["cached", "live"],
            format_func=lambda m: "🎞️ Cached timeline" if m == "cached" else "📡 Live GSI",
            horizontal=True,
            help="Cached steps through demo_match.json so the tab always demos. "
            "Live polls the GSI server at /latest.",
        )
    with ctrl2:
        auto = st.toggle("Auto-advance", value=False,
                         help="Fire a coaching turn roughly every 60 game-seconds.")
        interval = st.slider("Refresh (s)", 2, 10, 4) if auto else 4
    with ctrl3:
        b1, b2 = st.columns(2)
        step_now = b1.button("▶ Coach next turn", use_container_width=True)
        if b2.button("↺ Reset feed", use_container_width=True):
            st.session_state.live_feed = []
            st.session_state.cached_step = -1
            st.session_state.last_turn_clock = None
            st.rerun()

    cached_timeline = build_cached_timeline(match_data)

    def fire_turn():
        """Pull the next game state for the chosen source and coach on it."""
        if source_mode == "cached":
            nxt = st.session_state.cached_step + 1
            if nxt >= len(cached_timeline):
                return False  # timeline exhausted
            state = get_game_state("cached", step=nxt, timeline=cached_timeline)
            st.session_state.cached_step = nxt
        else:
            state = get_game_state("live", url=GSI_URL)
            if not state.get("ok"):
                st.session_state._live_note = state.get("note", "GSI unreachable")
                return False
            # Only coach at phase boundary or ~60 game-seconds since last turn.
            last = st.session_state.last_turn_clock
            if last is not None and (state["clock_s"] - last) < 60 and st.session_state.live_feed:
                last_phase = st.session_state.live_feed[-1]["phase"]
                if state["phase"] == last_phase:
                    return False
        result = run_coach_turn(state["match_like"], force_cached=fallback)
        append_turn(state, result)
        return True

    if step_now:
        fired = fire_turn()
        if not fired and source_mode == "cached":
            st.toast("Cached timeline complete — reset to replay.")

    # Live status row
    if source_mode == "live":
        live_state = get_game_state("live", url=GSI_URL)
        if live_state.get("ok"):
            cur_clock, cur_phase = live_state["clock_s"], live_state["phase"]
        else:
            cur_clock, cur_phase = 0, "waiting for bot match"
    else:
        idx = max(0, st.session_state.cached_step)
        if cached_timeline:
            snap = cached_timeline[min(idx, len(cached_timeline) - 1)]
            cur_clock, cur_phase = snap["clock_s"], snap["phase"]
        else:
            cur_clock, cur_phase = 0, game_phase(0)

    s1, s2, s3 = st.columns(3)
    s1.metric("Game clock", fmt_clock(cur_clock))
    s2.metric("Phase", cur_phase)
    s3.metric("Coaching turns", len(st.session_state.live_feed))

    if source_mode == "live" and not check_endpoint(GSI_URL, 1.0)["up"]:
        st.info("No live bot match detected on the GSI server. Switch to "
                "**🎞️ Cached timeline** for a guaranteed live-tab demo.")

    # Timeline of where the agent coached + deaths
    render_phase_timeline(
        turn_clocks=[t["clock_s"] for t in st.session_state.live_feed],
        death_events=match_data.get("deaths", []) if match_data else [],
    )

    st.markdown("#### 📣 Coaching feed")
    if not st.session_state.live_feed:
        st.caption("No turns yet — press **▶ Coach next turn** or enable **Auto-advance**.")
    for turn in reversed(st.session_state.live_feed):
        src = ("🛟 cached" if turn["source"] == "cached" else "🧠 model")
        lat = "cached" if turn["source"] == "cached" else f"{turn['latency_s']:.2f}s"
        st.markdown(
            f'<div class="rs-feed"><div class="meta">'
            f'⏱ {fmt_clock(turn["clock_s"])} · {turn["phase"]} · {src} · {lat} · {turn["wall"]}'
            f"</div></div>",
            unsafe_allow_html=True,
        )
        with st.expander(f"Coaching @ {fmt_clock(turn['clock_s'])} — {turn['phase']}",
                         expanded=(turn is st.session_state.live_feed[-1])):
            st.markdown(turn["markdown"])

    # Auto-advance loop (dependency-free): coach, then rerun after the interval.
    if auto:
        fire_turn()
        time.sleep(interval)
        st.rerun()

# ============================================================================
# COACH MEMORY (bottom, spans the app)
# ============================================================================
st.divider()
st.subheader("💾 Coach Memory")
render_coach_memory()
