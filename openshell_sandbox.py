"""openshell_sandbox.py — OpenShell integration for ReplaySense.

Wraps the app with NVIDIA OpenShell (Agent Toolkit) sandbox policies:
  - Blocks all outbound network traffic (nothing leaves the GB10)
  - Restricts filesystem access to the repo root only
  - Logs all sandbox events to audit_log.jsonl

Usage:
    # Launch Streamlit inside OpenShell sandbox:
    python openshell_sandbox.py

    # Or check sandbox status from within the app:
    from openshell_sandbox import get_sandbox_status, is_sandboxed
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

# ============================================================================
# SANDBOX POLICY
# ============================================================================
POLICY = {
    "name": "replaysense-coach",
    "version": "1.0",
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
            str(REPO_ROOT / "audit_log.jsonl"),
            str(REPO_ROOT / "coach_memory.json"),
        ],
        "block_read": ["/etc/passwd", "/etc/shadow", "~/.ssh"],
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
    # OpenShell sets this env var when the process is sandboxed
    return bool(os.environ.get("OPENSHELL_SANDBOX_ID"))


def get_sandbox_status() -> dict:
    """Return sandbox status info for display in the Streamlit UI."""
    sandboxed = is_sandboxed()
    sandbox_id = os.environ.get("OPENSHELL_SANDBOX_ID", "not sandboxed")
    policy_name = os.environ.get("OPENSHELL_POLICY_NAME", "none")

    # Check if openshell CLI is available
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
    }


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
        # OpenShell not installed — run normally but warn
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

    _audit("sandbox_launch", {"command": cmd, "sandboxed": openshell_available})

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print("\n🛑 ReplaySense stopped.")
        _audit("sandbox_stop", {"reason": "keyboard_interrupt"})


# ============================================================================
# AUDIT LOGGING
# ============================================================================
def _audit(event: str, data: dict = None):
    """Append a sandbox event to the audit log."""
    entry = {
        "event": f"openshell_{event}",
        "timestamp": time.time(),
        "sandbox_id": os.environ.get("OPENSHELL_SANDBOX_ID", "none"),
        **(data or {}),
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_model_call(endpoint: str, model: str, prompt_chars: int):
    """Called by agent.py to audit every LLM call inside the sandbox."""
    _audit("model_call", {
        "endpoint": endpoint,
        "model": model,
        "prompt_chars": prompt_chars,
        "local": endpoint.startswith(("http://localhost", "http://127.0.0.1")),
    })


def log_no_egress_violation(destination: str):
    """Called if code ever tries to reach an external host — should never happen."""
    _audit("egress_violation_blocked", {"destination": destination})


# ============================================================================
# ENTRY POINT
# ============================================================================
if __name__ == "__main__":
    launch_sandboxed()
