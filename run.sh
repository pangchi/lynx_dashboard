#!/bin/bash
# =============================================================================
#  BriskHeat LYNX Dashboard – Run with venv
#  Use this to start the app manually (outside of systemd).
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[ERROR] Virtual environment not found at $VENV_DIR"
    echo "        Run ./install.sh first."
    exit 1
fi

if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    echo "[ERROR] $VENV_DIR/bin/python not found."
    exit 1
fi

echo "[INFO]  Activating venv: $VENV_DIR"
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

echo "[INFO]  Python: $(which python)"
echo "[INFO]  Starting LYNX Dashboard..."
echo ""

exec python "$SCRIPT_DIR/app.py" "$@"
