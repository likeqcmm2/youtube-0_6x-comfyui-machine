#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$ROOT_DIR/installer/vast_comfyui_infinite_ltx_installer.sh"
WORKFLOW_DIR="/opt/comfyui-api-wrapper/workflows"

if [ "$(id -u)" -ne 0 ]; then
  echo "Please run as root on the Vast instance." >&2
  exit 1
fi

if [ ! -f "$INSTALLER" ]; then
  echo "Missing installer: $INSTALLER" >&2
  exit 1
fi

echo "==> Running ComfyUI + InfiniteTalk + LTX installer"
bash "$INSTALLER"

echo "==> Installing production workflow Python dependencies"
# The production runner reads the daily storyboard from .xlsx files.
source /venv/main/bin/activate
pip install -U openpyxl

echo "==> Installing packaged ComfyUI workflows"
mkdir -p "$WORKFLOW_DIR"
cp -f "$ROOT_DIR/workflows/LTX_I2V_FFLF_85frames_input_enabled.json" "$WORKFLOW_DIR/"
cp -f "$ROOT_DIR/workflows/Wan InfiniteTalk - Duration 4, 5, 6 Seconds.json" "$WORKFLOW_DIR/"

echo "==> Verifying workflow files"
test -s "$WORKFLOW_DIR/LTX_I2V_FFLF_85frames_input_enabled.json"
test -s "$WORKFLOW_DIR/Wan InfiniteTalk - Duration 4, 5, 6 Seconds.json"

echo "==> Done"
echo "ComfyUI:     http://127.0.0.1:18188"
echo "API wrapper: http://127.0.0.1:18288"
echo "Run production with:"
echo "  python3 $ROOT_DIR/ltx_comfyui_workflow.py --resources /root/Resources --first-scene 1 --last-scene YOUR_LAST_SCENE"
