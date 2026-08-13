# FloodGate Edge Brain — On-Device AI Decision Layer

The AI half of **FloodGate**, an end-to-end flood & clog detection system
(ESP32 edge sensing → MQTT → Raspberry Pi gateway → Supabase dashboard).
This folder adds a **local AI decision layer** to the Pi gateway using
**Needle 2**, a 14 MB / 45M-parameter tool-calling LLM from Cactus Compute
that runs entirely on the Pi 4/5 (no GPU, no internet needed for inference).
A deterministic rule engine runs in parallel as a safety net, and ambiguous
/ low-confidence / faulty-sensor situations escalate to a cloud model — a
genuine edge-cloud collaboration architecture.

It consumes the **exact MQTT payload the ESP32 firmware publishes**
(`device_id`, `water_depth` in meters, `atm_pressure_hpa`, `ambient_temp_c`,
`status`) on the main topic (`EXAMPLE_TELEMETRY`) — so the AI
plugs into the existing firmware, bridge, and dashboard **without changing
any of them**. `fg_core.py` is the single source of truth for the thresholds,
payload parsing, feature extraction, rule engine, and the exact LLM prompt
format shared by training, runtime, and evaluation.

See `../document/DESIGN.md` for the full system design and
`../document/TUTORIAL.md` for the learning-oriented walkthrough.

## Quick start on the Pi (Raspberry Pi 4 or 5, 64-bit OS)

```bash
# inference-only deps (no jax — the Pi doesn't train)
pip install -r requirements-pi.txt
python pin_engine.py                        # REQUIRED — pins the engine that reads our .cact

# verify with the built-in 4-phase simulator (normal -> storm -> flood -> sensor fault)
NEEDLE_WEIGHTS=floodgate.cact python edge_brain.py --simulate

# live: subscribe to the main MQTT topic; the existing ESP32 firmware feeds us as-is
NEEDLE_WEIGHTS=floodgate.cact python edge_brain.py --mqtt
#   status API: http://<pi-ip>:8090/status   (dashboard stays on :8080)
```

Expected decision lines (every ~10 s):

```
[14:02:31] edge=report_all_clear     rule=report_all_clear     AGREE   42ms
[14:03:41] edge=issue_storm_watch    rule=issue_storm_watch    AGREE   38ms
[14:04:52] edge=issue_flood_warning  rule=issue_flood_warning  AGREE   40ms
[14:05:03] edge=escalate_to_cloud    rule=escalate_to_cloud    AGREE   41ms
```

One-command deployment (installs the systemd service, auto-restart):

```bash
# on the Mac:  ./setup_mac.sh train   produces floodgate_pi_bundle.tar.gz
scp floodgate_pi_bundle.tar.gz pi@<pi-ip>:~
ssh pi@<pi-ip> 'tar xzf floodgate_pi_bundle.tar.gz && cd floodgate && bash deploy_pi.sh'
```

## Train on the MacBook, deploy to the Pi (recommended workflow)

```bash
./setup_mac.sh            # env (locked toolchain) -> 475-example dataset -> LoRA train -> 4-bit build -> smoke test -> Pi bundle
```

Each step is runnable individually (`./setup_mac.sh env|train|demo`):

1. **Environment** — locked toolchain (`requirements-locked.txt`:
   cactus-needle 2.0.2 + jax/flax/optax pins) + `pin_engine.py`. The lock is
   deliberate: 2.0.3 silently changed the trainer's LoRA target layers and
   quantization; everything here is built and validated on 2.0.2.
2. **Dataset** — `make_finetune_data.py --check` writes 475 rule-consistent
   flashcards (normal / storm / flood watch / flood warning / sensor-fault
   escalation / OpenWeather-driven storm watch / boundary cases), every
   query derived from `fg_core.build_prompt()`.
3. **Training** — `finetune_fp32.py finetune_data.jsonl --out floodgate_lora.pkl`
   (LoRA rank 16, 4 epochs, fp32 adapter — fixes the vendor trainer's
   loss=nan bug). Our runs: loss ~2.5 → 1.50.
4. **Build** — `needle build <base.pkl> --lora floodgate_lora.pkl --out floodgate.cact --bits 4`
   (~23 MB, W4A8). 2-bit was measurably worse — don't use it.
5. **Smoke test** — five canonical prompts through the tuned model (all
   clear, storm watch, flood warning, weather-driven storm, sensor fault).

## Evaluation (the honest numbers)

```bash
python eval_model.py --weights floodgate.cact    # tuned
python eval_model.py                             # base model (no weights)
```

200 held-out scenarios, rule engine as ground truth:

| Class | Base | Tuned |
|---|---|---|
| report_all_clear | 0% | **85%** |
| issue_storm_watch | 80% | 70% |
| flood watch / warning / escalate | 0% | 0% |
| **Overall** | **16%** | **31%** |

Honest reading: the 45M model learns the calm/pressure classes well; the
water-threshold classes stay with the deterministic rule engine (which is
exactly why the rules keep alert authority), and every edge/rule
disagreement escalates to the cloud tier.

## File map

| File | Role |
|---|---|
| `fg_core.py` | **single source of truth**: thresholds, parsing, features, rules, prompt |
| `edge_brain.py` | runtime: decision loop, Flask (`:8090`) + MQTT ingestion, simulator |
| `infer_worker.py` | crash-isolated inference subprocess (supervised by `InferenceClient`) |
| `make_finetune_data.py` | 475-example dataset generator (imports `fg_core`; `--check` verifies) |
| `finetune_fp32.py` | LoRA trainer (fp32 adapter fix for the vendor's loss=nan bug) |
| `eval_model.py` | held-out eval base vs tuned, confusion matrix, latency |
| `pin_engine.py` | engine/exporter match — pins the 2.0.0 engine (REQUIRED, once per machine) |
| `diagnose_cact.py` | .cact load-failure detective |
| `esp32_sender.ino` | optional HTTP alternative sender (same payload schema as the firmware) |
| `setup_mac.sh` | Mac pipeline: env → data → train → build → smoke → bundle |
| `deploy_pi.sh` | one-shot Pi 4/5 provisioning + systemd install |
| `floodgate-brain.service` | systemd unit (MQTT mode, auto-restart) |
| `requirements-locked.txt` | Mac training toolchain (pinned) |
| `requirements-pi.txt` | Pi inference-only dependencies |

## Known issues (all diagnosed and worked around — details in TUTORIAL.md)

1. **Vendor trainer loss=nan** — fp16 LoRA matrices underflow AdamW's eps.
   Use `finetune_fp32.py`.
2. **.cact format version skew** — exporters ≤ 2.0.2 write tag 0x05E12A82
   but the default engine reads 0x05E12A83. `pin_engine.py` pins the 2.0.0
   engine (run once per machine, Mac and Pi).
3. **cactus-needle 2.0.3 is not interchangeable** — changed LoRA target
   layers (10→5) and quantization; toolchain is locked to 2.0.2.
4. **macOS engine segfaults** after ~6-8 inferences with external weights —
   inference runs in a supervised, crash-restartable worker.
5. **2-bit quantization too lossy** — build with `--bits 4`.

First runs download the base model/engine from Hugging Face — do it on good
WiFi before the venue; everything after that is fully offline.
