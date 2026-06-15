"""openshell_sandbox.py — OpenShell integration for ReplaySense.

Wraps the app with NVIDIA OpenShell (Agent Toolkit) sandbox policies:
  - Blocks all outbound network traffic (nothing leaves the GB10)
  - Restricts filesystem access to the repo root only
  - Verifies every LLM call hits a local endpoint only
  - Logs all sandbox events to audit_log.jsonl in real time

Usage:
    # Launch Streamlit inside OpenShell sandbox:
    python openshell_sandbox.py

    # Check sandbox status from within the app:
    from openshell_sandbox import get_sandbox_status, is_sandboxed

    # Verify an endpoint is local before calling it:
    from openshell_sandbox import verify_local_endpoint

    # Get the last N audit events for the live dashboard:
    from openshell_sandbox import get_audit_events
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent
AUDIT_LOG = REPO_ROOT / "audit_log.jsonl"
POLICY_FILE = REPO_ROOT / "openshell_policy.json"

# Local endpoint prefixes — anything else is a violation
_LOCAL_PREFIXES = ("http://localhost", "http://127.0.0.1", "http://0.0.0.0")

# ============================================================================
# SANDBOX POLICY
# ============================================================================
POLICY = {
    "name": "replaysense-coach",
    "version": "1.1",
    "description": "ReplaySense local-only Dota 2 coach sandbox",
    "network": {
        "egress": "block",           # block ALL outbound internet traffic
        "ingress_allow": [
            "127.0.0.1:8501",        # Streamlit dashboard
            "127.0.0.1:53000",       # GSI server
            "127.0.0.1:11434",       # Ollama inference
            "127.0.0.1:8642",        # Hermes API
        ],
        "dns": "block",              # no DNS lookups
    },
    "filesystem": {
        "allow_read": [str(REPO_ROOT)],
        "allow_write": [
            str(REPO_ROOT / "episodic_memory"),
            str(REPO_ROOT / "chroma_store"),
            str(REPO_ROOT / "audit_log.jsonl"),
            str(REPO_ROOT / "coach_memory.json"),
            str(REPO_ROOT / "openshell_policy.json"),
        ],
        "block_read": ["/etc/passwd", "/etc/shadow", "~/.ssh"],
        "encrypt_episodic_memory": True,   # player session data protected at rest
    },
    "process": {
        "allow_spawn": ["python3", "streamlit", "ollama"],
        "block_spawn": ["curl", "wget", "ssh", "nc"],
    },
}


def write_policy():
    """Write the sandbox policy JSON to disk for OpenShell to consume."""
    POLICY_FILE.write_text(json.dumps(POLICY, indent=2))
    _audit("policy_written", {"policy_file": str(POLICY_FILE)})


# ============================================================================
# SANDBOX DETECTION
# ============================================================================
def is_sandboxed() -> bool:
    """Return True if running inside an OpenShell sandbox."""
    return bool(os.environ.get("OPENSHELL_SANDBOX_ID"))


def _check_openshell_available() -> bool:
    """Check if the openshell CLI is installed on this machine."""
    try:
        result = subprocess.run(
            ["openshell", "--version"],
            capture_output=True, timeout=3
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_sandbox_status() -> dict:
    """Return sandbox status info for display in the Streamlit UI."""
    sandboxed = is_sandboxed()
    sandbox_id = os.environ.get("OPENSHELL_SANDBOX_ID", "not sandboxed")
    policy_name = os.environ.get("OPENSHELL_POLICY_NAME", "none")
    openshell_available = _check_openshell_available()

    return {
        "sandboxed": sandboxed,
        "sandbox_id": sandbox_id,
        "policy_name": policy_name,
        "openshell_available": openshell_available,
        "network_egress": "BLOCKED" if sandboxed else "unrestricted (not sandboxed)",
        "data_stays_local": True,  # always true — we never call cloud APIs
        "policy_file": str(POLICY_FILE) if POLICY_FILE.exists() else "not written",
        "badge": "🔒 OpenShell Sandboxed" if sandboxed else "⚠️ Not sandboxed (run via launch.sh)",
        "episodic_memory_protected": POLICY["filesystem"].get("encrypt_episodic_memory", False),
    }


# ============================================================================
# ENDPOINT VERIFICATION (integration point 1)
# ============================================================================
def verify_local_endpoint(url: str) -> str:
    """Assert the URL is localhost-only. Returns the URL if safe, raises if not.

    Called by agent.py and rag.py before every outbound request. Ensures that
    even if an env var is misconfigured, we never phone home.
    """
    if url.startswith(_LOCAL_PREFIXES):
        return url
    # Non-local endpoint detected — log and block
    log_egress_violation(url)
    raise ValueError(
        f"🚫 OpenShell: blocked non-local endpoint '{url}'. "
        f"ReplaySense is LOCAL ONLY — all inference must hit localhost."
    )


# ============================================================================
# AUDIT LOGGING (integration point 2)
# ============================================================================
def _audit(event: str, data: dict = None):
    """Append a sandbox event to the audit log. Never raises."""
    entry = {
        "event": f"openshell_{event}",
        "timestamp": time.time(),
        "sandbox_id": os.environ.get("OPENSHELL_SANDBOX_ID", "none"),
        **(data or {}),
    }
    try:
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never crash the app due to audit failure


def log_model_call(endpoint: str, model: str, prompt_chars: int):
    """Called by agent.py before every LLM request."""
    _audit("model_call", {
        "endpoint": endpoint,
        "model": model,
        "prompt_chars": prompt_chars,
        "local": endpoint.startswith(_LOCAL_PREFIXES),
    })


def log_embed_call(endpoint: str, model: str, text_chars: int):
    """Called by rag.py before every embedding request."""
    _audit("embed_call", {
        "endpoint": endpoint,
        "model": model,
        "text_chars": text_chars,
        "local": endpoint.startswith(_LOCAL_PREFIXES),
    })


def log_gsi_poll(url: str, ok: bool):
    """Called by live_loop.py after every GSI poll."""
    _audit("gsi_poll", {
        "url": url,
        "ok": ok,
        "local": url.startswith(_LOCAL_PREFIXES),
    })


def log_memory_write(match_id: str, path: str):
    """Called by memory.py when persisting a session."""
    _audit("memory_write", {
        "match_id": match_id,
        "path": path,
        "protected": POLICY["filesystem"].get("encrypt_episodic_memory", False),
    })


def log_egress_violation(destination: str):
    """Called when a non-local endpoint is attempted — should never happen."""
    _audit("egress_violation_blocked", {"destination": destination})


# ============================================================================
# AUDIT READER — for the live dashboard (integration point 3)
# ============================================================================
def get_audit_events(limit: int = 20) -> list:
    """Return the last N audit events from the log, newest first.

    Used by app.py to power the live sandbox audit dashboard.
    Returns [] if the log doesn't exist yet — never raises.
    """
    if not AUDIT_LOG.exists():
        return []
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
        events = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except Exception:
                continue
            if len(events) >= limit:
                break
        return events
    except Exception:
        return []


def get_audit_summary() -> dict:
    """Aggregate stats over the full audit log for the sidebar."""
    events = get_audit_events(limit=1000)
    model_calls = [e for e in events if e.get("event") == "openshell_model_call"]
    embed_calls = [e for e in events if e.get("event") == "openshell_embed_call"]
    violations = [e for e in events if e.get("event") == "openshell_egress_violation_blocked"]
    gsi_polls = [e for e in events if e.get("event") == "openshell_gsi_poll"]
    all_local = all(e.get("local", True) for e in model_calls + embed_calls)
    return {
        "total_model_calls": len(model_calls),
        "total_embed_calls": len(embed_calls),
        "total_gsi_polls": len(gsi_polls),
        "violations_blocked": len(violations),
        "all_inference_local": all_local,
        "last_model_call": model_calls[0] if model_calls else None,
    }


# ============================================================================
# LAUNCH INSIDE SANDBOX
# ============================================================================
def launch_sandboxed():
    """Launch the ReplaySense Streamlit app inside an OpenShell sandbox."""
    write_policy()
    _audit("sandbox_launch_attempt", {"policy": POLICY_FILE.name})

    openshell_available = _check_openshell_available()

    if openshell_available:
        print("🔒 Launching ReplaySense inside OpenShell sandbox...")
        print(f"   Policy: {POLICY_FILE}")
        print("   Network egress: BLOCKED")
        print("   All inference stays on GB10\n")

        cmd = [
            "openshell", "run",
            "--policy", str(POLICY_FILE),
            "--no-network-egress",
            "--sandbox-name", "replaysense-coach",
            "--",
            "streamlit", "run", str(REPO_ROOT / "app.py"),
            "--server.port", "8501",
            "--server.address", "0.0.0.0",
        ]
    else:
        print("⚠️  OpenShell not found. Running WITHOUT sandbox.")
        print("   Install OpenShell (NVIDIA Agent Toolkit) for full security story.")
        print("   App still runs fully local — no cloud SDKs used.\n")
        print("🎮 Launching ReplaySense (local mode)...")

        cmd = [
            sys.executable, "-m", "streamlit", "run",
            str(REPO_ROOT / "app.py"),
            "--server.port", "8501",
            "--server.address", "0.0.0.0",
        ]

    _audit("sandbox_launch", {"sandboxed": openshell_available})

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print("\n🛑 ReplaySense stopped.")
        _audit("sandbox_stop", {"reason": "keyboard_interrupt"})


# ============================================================================
# ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    launch_sandboxed()
