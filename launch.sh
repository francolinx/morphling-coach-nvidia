#!/bin/bash
# launch.sh — ReplaySense launcher for GB10 demo day
# Starts GSI server + Streamlit inside OpenShell sandbox (or local fallback)

set -e
REPO="$(cd "$(dirname "$0")" && pwd)"

echo "======================================================"
echo "  ReplaySense — Local AI Coach for Dota 2"
echo "  Dell Pro Max with GB10 · NemoClaw + Hermes"
echo "======================================================"
echo ""

# ── Model endpoint config ──────────────────────────────────
# Change these to switch between Hermes (primary) and Ollama (fallback)
export REPLAYSENSE_MODEL_URL="${REPLAYSENSE_MODEL_URL:-http://localhost:11434/api/chat}"
export REPLAYSENSE_MODEL_NAME="${REPLAYSENSE_MODEL_NAME:-gemma4}"
export REPLAYSENSE_API_KEY="${REPLAYSENSE_API_KEY:-}"
export REPLAYSENSE_GSI_URL="${REPLAYSENSE_GSI_URL:-http://localhost:53000/latest}"

echo "📡 Model endpoint : $REPLAYSENSE_MODEL_URL"
echo "🤖 Model name     : $REPLAYSENSE_MODEL_NAME"
echo "🎮 GSI server     : $REPLAYSENSE_GSI_URL"
echo ""

# ── Start GSI server in background ────────────────────────
echo "▶ Starting GSI server on :53000..."
cd "$REPO"
python3 -m gsi.gsi_server &
GSI_PID=$!
sleep 1
if kill -0 $GSI_PID 2>/dev/null; then
    echo "✅ GSI server running (PID $GSI_PID)"
else
    echo "⚠️  GSI server failed to start — live tab will show cached data"
fi

# ── Launch app via OpenShell sandbox ──────────────────────
echo ""
echo "▶ Launching ReplaySense dashboard..."
echo "   Access at: http://localhost:8501"
echo ""

python3 "$REPO/openshell_sandbox.py"

# ── Cleanup ───────────────────────────────────────────────
echo ""
echo "🛑 Shutting down GSI server..."
kill $GSI_PID 2>/dev/null || true
echo "Done."
