"""ReplaySense UI — local AI coach demo."""

import json
import time
from pathlib import Path

import streamlit as st

from agent import coach_match, MODEL_NAME, MODEL_URL

st.set_page_config(
    page_title="ReplaySense — Local AI Coach",
    page_icon="🎮",
    layout="wide",
)

st.title("🎮 ReplaySense")
st.caption(f"Local AI coaching for competitive esports teams · Model: `{MODEL_NAME}` · Endpoint: `{MODEL_URL}`")

# Sidebar: hardware story
with st.sidebar:
    st.header("🔒 Privacy")
    st.success("✓ All inference local\n\n✓ No network calls\n\n✓ Team data never leaves device")
    st.divider()
    st.header("📊 Stack")
    st.code(f"Model: {MODEL_NAME}\nHost:  {MODEL_URL}\nRAG:   21 corpus files\nLog:   audit_log.jsonl")

# Main column
col1, col2 = st.columns([1, 2])

with col1:
    st.subheader("Load Match")
    demo_path = Path("data/demo_match.json")
    if demo_path.exists():
        match_data = json.loads(demo_path.read_text())
        st.json(match_data, expanded=False)
    else:
        st.warning("data/demo_match.json not found")
        match_data = None

    analyze = st.button("▶ Analyze Match", type="primary", use_container_width=True, disabled=match_data is None)

with col2:
    st.subheader("Coaching Report")
    output_area = st.empty()

    if analyze and match_data:
        with st.spinner("Coach analyzing... (local inference)"):
            start = time.time()
            try:
                response = coach_match(match_data)
                latency = time.time() - start
                output_area.markdown(response)
                st.success(f"✓ Coaching complete in {latency:.1f}s — fully on-device")
            except Exception as e:
                output_area.error(f"Coach error: {e}\n\nIs the local model running at {MODEL_URL}?")

st.divider()

# Memory + audit log preview
mem_col, audit_col = st.columns(2)
with mem_col:
    st.subheader("💾 Coach Memory")
    mem_path = Path("coach_memory.json")
    if mem_path.exists():
        st.json(json.loads(mem_path.read_text()))
    else:
        st.caption("No memory yet — run a coaching session")

with audit_col:
    st.subheader("📋 Audit Log (last 5)")
    audit_path = Path("audit_log.jsonl")
    if audit_path.exists():
        lines = audit_path.read_text().strip().split("\n")[-5:]
        for line in lines:
            if line:
                st.code(line, language="json")
    else:
        st.caption("No events logged yet")
