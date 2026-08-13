#!/usr/bin/env bash
# ============================================================================
# FloodGate Edge Brain — MacBook Pro setup, LoRA fine-tune, and Pi packaging
#
#   ./setup_mac.sh          full pipeline: env -> data -> train -> build -> test
#   ./setup_mac.sh env      just create the environment
#   ./setup_mac.sh train    regenerate data + retrain + rebuild
#   ./setup_mac.sh demo     run the simulator demo with the tuned model
#
# Requires: Python 3.10+ (check: python3 --version). Internet needed once,
# to download the base model from Hugging Face.
#
# All logic (thresholds, prompt format, rule engine) lives in fg_core.py —
# the dataset generator, runtime and eval all import it, so nothing can drift.
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"
STEP="${1:-all}"

VENV=".venv"
ADAPTER="floodgate_lora.pkl"
CACT="floodgate.cact"

env_setup() {
  echo "==> Creating Python environment..."
  python3 -m venv "$VENV"
  source "$VENV/bin/activate"
  pip install --quiet --upgrade pip
  # LOCKED toolchain — validated end-to-end for this project (see DESIGN.md):
  # cactus-needle 2.0.3 changed the LoRA target groups (10 -> 5) and the
  # quantization scheme; every artifact in this repo is built with 2.0.2 +
  # the pinned 2.0.0 engine. Keep the lock on the Pi too.
  pip install --quiet -r requirements-locked.txt
  echo "==> Ensuring engine matches the .cact exporter (pin_engine pins the"
  echo "    2.0.0 engine dylib, which reads the 0x05E12A82 format 2.0.2 writes)..."
  python3 pin_engine.py
  echo "==> Environment ready ($(python3 --version), $(pip show cactus-needle | grep ^Version))"
}

train() {
  source "$VENV/bin/activate"
  echo "==> Generating fine-tuning dataset (~475 FloodGate examples)..."
  python3 make_finetune_data.py
  echo "==> Verifying dataset consistency with fg_core + live tools..."
  python3 make_finetune_data.py --check

  echo "==> LoRA fine-tuning Needle 2 (fp32 adapter, rank 16, 3 epochs)."
  echo "    Uses finetune_fp32.py — fixes the loss=nan bug in 'needle finetune'"
  echo "    on fp16 checkpoints. Expect a few minutes on Apple Silicon..."
  rm -f "$ADAPTER" "$CACT"        # remove any poisoned artifacts from failed runs
  python3 finetune_fp32.py finetune_data.jsonl --out "$ADAPTER"

  echo "==> Merging LoRA + exporting quantized deployable: $CACT"
  # The base checkpoint lands in ./checkpoints/ on first finetune run.
  BASE_CKPT=$(ls -t checkpoints/*.pkl 2>/dev/null | head -1 || true)
  if [ -z "$BASE_CKPT" ]; then
    echo "!! Could not find base checkpoint in ./checkpoints/."
    echo "   Run 'needle finetune --help' / check the repo README for the"
    echo "   checkpoint location on your version, then run manually:"
    echo "   needle build <base.pkl> --lora $ADAPTER --out $CACT --bits 2"
    exit 1
  fi
  needle build "$BASE_CKPT" --lora "$ADAPTER" --out "$CACT" --bits 4
  echo "==> Built $CACT ($(du -h "$CACT" | cut -f1))  [4-bit: 2-bit quantization"
  echo "    measurably degraded the retrained model — see DESIGN.md §10]"
}

smoke_test() {
  source "$VENV/bin/activate"
  echo "==> Smoke test: 5 prompts through the tuned model (incl. fault + weather)..."
  python3 - <<'EOF'
import edge_brain, fg_core

client = edge_brain.InferenceClient(weights="floodgate.cact")
tests = [
    ("all clear",  dict(pressure_hpa=1015.2, pressure_trend_hpa_per_hr=0.3,
                        temperature_c=22.0, water_level_cm=8.0,
                        water_rise_cm_per_min=0.0, sensor_status="OK",
                        weather_high_risk=0, device_id="ESP32-FloodGate",
                        samples_in_window=99)),
    ("storm watch", dict(pressure_hpa=1002.1, pressure_trend_hpa_per_hr=-4.5,
                         temperature_c=21.0, water_level_cm=9.0,
                         water_rise_cm_per_min=0.1, sensor_status="OK",
                         weather_high_risk=0, device_id="ESP32-FloodGate",
                         samples_in_window=99)),
    ("flood warning", dict(pressure_hpa=1004.0, pressure_trend_hpa_per_hr=-1.0,
                           temperature_c=20.0, water_level_cm=41.0,
                           water_rise_cm_per_min=1.2, sensor_status="OK",
                           weather_high_risk=0, device_id="ESP32-FloodGate",
                           samples_in_window=99)),
    ("weather storm", dict(pressure_hpa=1012.0, pressure_trend_hpa_per_hr=-0.4,
                           temperature_c=22.0, water_level_cm=10.0,
                           water_rise_cm_per_min=0.1, sensor_status="OK",
                           weather_high_risk=1, device_id="ESP32-FloodGate",
                           samples_in_window=99)),
    ("sensor fault", dict(pressure_hpa=1010.0, pressure_trend_hpa_per_hr=-0.2,
                          temperature_c=22.0, water_level_cm=15.0,
                          water_rise_cm_per_min=0.0, sensor_status="ADC_ERROR",
                          weather_high_risk=0, device_id="ESP32-FloodGate",
                          samples_in_window=99)),
]
for label, f in tests:
    resp = client.complete(fg_core.build_prompt(f))
    calls = resp.get("function_calls") or []
    name = calls[0]["name"] if calls else "no_call"
    rule = fg_core.rule_engine(f)
    mark = "OK" if name == rule else "MISMATCH"
    print(f"  {label:14s} -> {name:20s} rule={rule:20s} {mark}")
client.kill()
EOF
}

demo() {
  source "$VENV/bin/activate"
  echo "==> Running full simulator demo with the tuned model (Ctrl-C to stop)"
  NEEDLE_WEIGHTS="$CACT" python3 edge_brain.py --simulate
}

package_for_pi() {
  echo "==> Packaging deployment bundle for the Pi 4/5..."
  tar czf floodgate_pi_bundle.tar.gz \
      "$CACT" fg_core.py edge_brain.py infer_worker.py pin_engine.py \
      eval_model.py diagnose_cact.py deploy_pi.sh floodgate-brain.service \
      requirements-locked.txt requirements-pi.txt README.md TUTORIAL.md DESIGN.md
  cat <<'EOF'
==> Done. Copy to the Pi and run:

  scp floodgate_pi_bundle.tar.gz pi@<pi-ip>:~
  ssh pi@<pi-ip>
  tar xzf floodgate_pi_bundle.tar.gz
  bash deploy_pi.sh                # venv + deps + engine pin + systemd service

Live endpoints once deployed:
  http://<pi-ip>:8080/status       # current features + last decision
  http://<pi-ip>:8080/health       # ok + mqtt listener state

The service subscribes to the main project's MQTT topic
(umd/cpse/floodgate/telemetry) so the EXISTING floodgate_firmware.ino works
unchanged. To run manually instead of the service:
  NEEDLE_WEIGHTS=floodgate.cact fg/bin/python edge_brain.py --mqtt
EOF
}

case "$STEP" in
  env)   env_setup ;;
  train) train && smoke_test && package_for_pi ;;
  demo)  demo ;;
  all)   env_setup && train && smoke_test && package_for_pi ;;
  *)     echo "usage: $0 [env|train|demo|all]" ; exit 1 ;;
esac
