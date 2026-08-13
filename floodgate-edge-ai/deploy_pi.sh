#!/usr/bin/env bash
# ============================================================================
# deploy_pi.sh — one-shot provisioning of the FloodGate Edge Brain on a
# Raspberry Pi 4 or 5 (Raspberry Pi OS Bookworm 64-bit, aarch64).
#
#   REQUIREMENT: 64-bit Raspberry Pi OS. The Needle engine is an aarch64
#   binary; 32-bit ARM is not supported. Check with:  uname -m   (=> aarch64)
#
# Run it FROM the directory the bundle was extracted into, e.g.:
#
#   # on the Mac:
#   scp floodgate_pi_bundle.tar.gz pi@<pi-ip>:~
#   ssh pi@<pi-ip>
#   tar xzf floodgate_pi_bundle.tar.gz && cd floodgate
#   bash deploy_pi.sh
#
# It: creates a venv, installs the inference-only deps (no jax — the Pi only
# runs inference), pins the Needle engine (REQUIRED — see pin_engine.py /
# TUTORIAL section 9), evaluates the tuned model on the Pi, runs a short
# simulator smoke test, then installs + starts the systemd service that runs
# edge_brain.py in --mqtt mode (subscribing to the main project's MQTT topic)
# with the Flask status API on :8090 (the main web dashboard keeps :8080).
# It coexists with the main project's floodgate-bridge and floodgate-web
# services.
#
# Pi 4 note: inference is ~2-3x slower than a Pi 5 (Cortex-A72 vs A76) —
# expect roughly 150-350 ms per decision, well inside the 10 s decision
# period. The 2 GB Pi 4 is fine (engine peak RAM ~75 MB).
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

echo "==> [0/6] Platform check"
case "$(uname -m)" in
  aarch64|arm64) echo "    aarch64 OK (64-bit OS)" ;;
  *) echo "    ERROR: $(uname -m) detected. Install 64-bit Raspberry Pi OS"
     echo "    (Bookworm) — the Needle engine does not ship a 32-bit ARM build."
     exit 1 ;;
esac

echo "==> [1/6] System packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

echo "==> [2/6] Python venv + inference-only dependencies (no jax needed)"
python3 -m venv fg
fg/bin/pip install --quiet --upgrade pip
fg/bin/pip install --quiet -r requirements-pi.txt

echo "==> [3/6] Pin Needle engine 2.0.0 (REQUIRED — reads our .cact format)"
fg/bin/python pin_engine.py

echo "==> [4/6] Verify the tuned model on the Pi (accuracy + latency)"
fg/bin/python eval_model.py --weights floodgate.cact

echo "==> [4b/6] 60s simulator smoke test with the tuned model"
timeout 60 env NEEDLE_WEIGHTS=floodgate.cact fg/bin/python edge_brain.py --simulate \
  | head -20 || true

echo "==> [5/6] Install + start the systemd service"
# Render the unit with the correct user and home directory (defaults: pi)
FG_USER="${FG_USER:-$(whoami)}"
FG_HOME="${FG_HOME:-$HOME}"
FG_DIR="$(pwd)"
sed -e "s|__USER__|$FG_USER|g" \
    -e "s|__HOME__|$FG_HOME|g" \
    -e "s|__DIR__|$FG_DIR|g" \
    floodgate-brain.service | sudo tee /etc/systemd/system/floodgate-brain.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now floodgate-brain

sleep 6
echo
echo "==> [6/6] Status endpoint:"
curl -s http://127.0.0.1:8090/health || echo "(health check failed — see: journalctl -u floodgate-brain -n 50)"
echo
echo "=== FloodGate edge brain deployed ==="
echo "  status: http://$(hostname -I | awk '{print $1}'):8090/status"
echo "  logs:   journalctl -u floodgate-brain -f"
echo "  The ESP32 firmware (floodgate_firmware.ino) feeds the brain via MQTT —"
echo "  no firmware changes were needed. The main dashboard stays on :8080."
