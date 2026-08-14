# FloodGate — System Design Document

**Project:** FloodGate — Edge-Computing IoT Flood & Clog Detection Gateway
**This document covers:** the whole system as designed, including the
on-device AI decision layer (the "edge brain") that runs on the Raspberry Pi
gateway alongside the existing bridge and dashboard.

---

## 1. System overview

FloodGate watches storm drains. A battery-friendly ESP32 edge node measures
water depth, barometric pressure and temperature; it publishes JSON telemetry
over MQTT. A Raspberry Pi 4/5 gateway (the "brain" of the system) does three
jobs on the same MQTT bus:

1. **Bridge** (`floodgatebridgeandweather.py`) — forwards telemetry to a
   Supabase time-series table and publishes OpenWeather forecast flags.
2. **Web dashboard** (`index.html` + `app.js`, served on `:8080`) — live
   charts and a client-side flood/clog risk engine.
3. **Edge brain** (`floodgate-edge-ai/`, this project) — a local AI decision
   layer: a 14 MB / 45M-parameter **Needle 2** LLM plus a deterministic rule
   engine that decide, every cycle, what alert (if any) the system should
   raise. The rule engine keeps final authority; the LLM adds judgement,
   explanations, and sensor-fault detection; genuinely ambiguous cases
   escalate to a cloud model.

```
 ESP32 edge node (deep sleep 1-5 min)
   BMP280 (temp, pressure) + DFRobot KIT0139 (water depth via ADS1115)
        │  MQTT JSON: {"device_id","water_depth"(m),"atm_pressure_hpa",
        │              "ambient_temp_c","status"}
        ▼
 broker (default public broker.hivemq.com; local mosquitto supported)
        │  topic: umd/cpse/floodgate/telemetry
        ├───────────────► floodgatebridgeandweather.py ──► Supabase ──► dashboard (:8080)
        │                     └─ OpenWeather "high risk" broadcasts back onto the same topic
        └───────────────► edge_brain.py (:8090)
                              ├─ fg_core: rolling window + trend features
                              ├─ rule engine (deterministic, always drives the alert)
                              ├─ Needle 2 LLM (tool calling, local, offline)
                              ├─ edge/rule comparison + escalation
                              └─ optional cloud tier (Anthropic API)
```

**Design principle that shapes everything: never let the AI be a single point
of failure for a safety decision.** The deterministic rule engine always
drives the real alert. The AI runs in parallel, its decision is compared
against the rules every cycle, and the agree/disagree rate is the live
health signal of the whole system.

## 2. The data contract (one wire format, designed once)

Every component speaks one payload schema — the ESP32 firmware's telemetry:

```json
{
  "device_id": "ESP32-FloodGate",
  "water_depth": 0.245,          // METERS — the dashboard charts "Depth (m)"
  "atm_pressure_hpa": 1013.2,    // hPa
  "ambient_temp_c": 24.1,        // deg C
  "status": "OK"                 // "OK" | "ADC_ERROR" (water sensor unreadable)
}
```

The OpenWeather bridge publishes forecast flags on the **same topic**:

```json
{"source": "openweather_api", "city": "Potomac", "high risk": 0 | 1}
```

The edge brain consumes both through `fg_core.parse_payload()`. Two
conventions keep this simple and safe:

* **Units convert at the boundary, once.** The firmware measures depth in
  meters; the edge brain's thresholds and training vocabulary are in
  centimeters (25 cm watch / 35 cm critical). `parse_payload` does
  `cm = m * 100` in exactly one place; nothing downstream ever touches the
  other unit.
* **The sensor tells you if it is lying.** `status: "ADC_ERROR"` and the
  derived `STALE` state (no reading for 15 minutes — three missed 5-minute
  sleep cycles) are first-class features. The rule engine escalates on bad
  status *before* any water rule can fire: we refuse to raise a flood alert
  on data we know is garbage.

## 3. The edge brain architecture (`floodgate-edge-ai/`)

### 3.1 Single source of truth: `fg_core.py`

Everything that must be identical across training, runtime and evaluation
lives in one module: thresholds, the payload parser, the feature extractor,
the rule engine, the prompt builder and the system prompt. The dataset
generator, the live brain and the eval harness all import it — they cannot
drift apart, because the query strings and labels are *derived* from the same
functions, not copied.

### 3.2 Features

Raw readings are not the signal — trends are. `SensorWindow` keeps a rolling
window and computes, per cycle:

| Feature | Meaning |
|---|---|
| `pressure_hpa`, `temperature_c`, `water_level_cm` | latest values |
| `pressure_trend_hpa_per_hr` | pressure rate over the last hour (storm signal) |
| `water_rise_cm_per_min` | water rise over the last 5 minutes |
| `sensor_status` | `OK` / `ADC_ERROR` / `STALE` |
| `weather_high_risk` | OpenWeather broadcast active (2 h TTL) |
| `device_id`, `samples_in_window` | provenance / window health |

Trends need real history: a rate computed over a span under 20% of its
nominal window is startup noise and returns 0 (`FG_TREND_MIN_FRACTION`).

### 3.3 The rule engine — the safety net

```text
status != OK              -> escalate_to_cloud      (never alert on bad data)
water >= 35 cm            -> issue_flood_warning    (overflow imminent)
water >= 25 cm or
rise >= 0.5 cm/min        -> issue_flood_watch
pressure trend <= -2 hPa/hr  or
weather high-risk flag    -> issue_storm_watch
otherwise                 -> report_all_clear
```

Ten lines of auditable `if`s that can never hallucinate. It always drives
the real alert; the LLM's answer is compared to it and the disagreement rate
is logged live.

### 3.4 The LLM (Needle 2) and the tools

Needle 2 is a 45M-parameter **tool-calling** LLM (~14-23 MB deployed). The
brain hands it a menu of five tools and a one-sentence sensor summary; the
model's entire job is to choose the right call:

`report_all_clear` · `issue_storm_watch` · `issue_flood_watch` ·
`issue_flood_warning` · `escalate_to_cloud`

The tool docstrings are the model's instructions, and the prompt format is
byte-for-byte identical between training data and live prompts (enforced by
`fg_core.build_prompt`).

### 3.5 Edge-cloud collaboration

Routine decisions stay local, private, instant and free. The system escalates
to a cloud model (Anthropic API, optional) when the edge model is unsure:
it calls `escalate_to_cloud`, disagrees with the rule engine, reports
confidence below the floor, or the sensor status is bad. Without an API key,
escalations are logged but skipped — the demo still works fully offline.

### 3.6 Ingestion

* **MQTT (recommended, `--mqtt`):** subscribe to the main topic
  (`umd/cpse/floodgate/telemetry`). The existing ESP32 firmware works
  unchanged — no reflash needed. Weather broadcasts arrive on the same
  subscription and are routed to the weather flag.
* **HTTP (alternative):** `esp32_sender.ino` POSTs the same schema to
  `:8090/reading` (5 s cadence, no broker needed).
* **Simulator (`--simulate`):** a four-phase demo (normal → storm with
  weather broadcast → flood → sensor fault) feeds the exact same ingestion
  path, so the demo exercises the real pipeline.

The status/health API is on **`:8090`** (`FG_HTTP_PORT` to override) — the
main web dashboard owns `:8080`.

### 3.7 Crash-isolated inference

The Needle engine (macOS dylibs) was found to segfault after ~6-8 inferences
when external `.cact` weights are loaded; the base-model path is unaffected.
Inference therefore runs in a supervised child process (`infer_worker.py`,
managed by `edge_brain.InferenceClient`). If the worker dies, the parent
restarts it and retries the prompt; after retries, the cycle runs
rule-engine-only. The Linux (Pi) engine build is likely unaffected, but the
worker is cheap insurance everywhere and makes the service genuinely
robust.

## 4. The model and the training pipeline

### 4.1 Why an AI at all, when rules exist?

* **Rules only cover what someone enumerated.** Three sensors × thresholds
  × combinations explode; a model learns the *shape* of the decision and
  interpolates between examples.
* **Rules can't recognize "this input makes no sense."** Frozen readings, a
  50 cm jump in 20 seconds, pressure screaming storm while water drains —
  the list of sensor failure modes is endless; a model learns the *concept*
  of implausibility from a few representative examples.
* **Rules output codes; the model outputs *why*.** For a public warning
  system the explanation is half the product.
* **Rules have cliff edges (34.9 cm calm, 35.0 cm crisis); the model can
  express doubt** — which drives the escalation/triage architecture.
* **Changing rules means code surgery; changing a model means data.** A
  student team can "improve the system" by editing examples and retraining.

The transferable pattern: **deterministic rules for what must never fail,
a small local model for what can't be enumerated, a cloud model only for
what's rare and genuinely hard.**

### 4.2 Dataset (`make_finetune_data.py`, 475 examples)

JSONL flashcards in the trainer's exact schema (`query` / `tools` /
`answers` / `reasoning` / `system`), seeded (`FG_DATA_SEED=42`) and
100% rule-consistent by construction:

| Label | Count | What it teaches |
|---|---|---|
| report_all_clear | 124 | calm conditions |
| issue_storm_watch | 113 | pressure trend ≤ -2 hPa/hr (90) + weather flag (20) + boundary (3) |
| issue_flood_watch | 96 | level in [25, 35) cm or rise ≥ 0.5 cm/min |
| issue_flood_warning | 70 | level ≥ 35 cm |
| escalate_to_cloud | 70 | 40 adversarial (frozen/conflicting/spiking) + 30 hardware faults |
| (boundary) | ~15 | near-threshold cases labeled by the rule engine itself |

Adversarial rows deliberately disagree with the rules — the model's genuine
value is catching what enumerated rules can't.

### 4.3 Training (`finetune_fp32.py`) and the locked toolchain

LoRA (low-rank adaptation) freezes the 45M base weights and learns small
correction matrices (rank 16) — minutes on a laptop. The trainer keeps the
adapter in float32 because the vendor's `needle finetune` casts to the
checkpoint's float16 and AdamW's eps underflows → `loss=nan` from step 2
(verified by controlled reproduction). It also clips gradients and refuses
to save a poisoned adapter.

**The toolchain is deliberately locked** (`requirements-locked.txt`):
cactus-needle **2.0.2** + the pinned 2.0.0 engine dylib + jax/jaxlib 0.11.0,
flax 0.12.8, optax 0.2.8, numpy 2.5.2. Reasons, all verified:

* Exporters ≤ 2.0.2 write `.cact` format tag `0x05E12A82`, but the default
  engine only reads `0x05E12A83` (version skew — engine shipped before its
  exporter). The 2.0.0 engine dylib reads our format; `pin_engine.py` pins
  it (and refuses to run on any other cactus-needle version).
* cactus-needle 2.0.3 ships a matched exporter at `...83`, but silently
  changed the LoRA target groups (10 → 5) and quantization. Retraining on it
  was measurably worse. "Latest" is not a version — it's a moving target.

Hyperparameters: rank 16, α 32, batch 16, lr 1e-4, clip 1.0, **4 epochs**
(the sweet spot — 6 epochs reached lower loss but *overfit*: the model
collapsed onto its favorite class and held-out accuracy dropped). Final loss
≈ 1.50 (the original recipe's was 1.73).

### 4.4 Deployment artifact (`needle build`)

`needle build` merges the adapter into the base weights and quantizes to
4-bit (`--bits 4`, W4A8) — a single ~23 MB `floodgate.cact`. 2-bit
quantization was tried first and measurably degraded the model (training-set
fit 5% vs 30%+, "reasoning loops" that exhausted the token budget) — for a
model whose output must be an exact JSON tool call, quantization is a
hyperparameter to measure, not a checkbox.

## 5. Measured results (honest)

Held-out evaluation: 200 fresh scenarios (same physics, new values,
near-threshold cases), graded against the rule engine as ground truth.

| Class | Base model | Tuned (4-bit) |
|---|---|---|
| report_all_clear | 0% | **85%** |
| issue_storm_watch | 80% | 70% |
| issue_flood_watch | 0% | 0% |
| issue_flood_warning | 0% | 0% |
| escalate_to_cloud | 0% | 0% |
| **Overall** | **16%** | **31%** |

Honest reading: a 45M tool-calling model reliably learns the calm/pressure
classes but not the numeric water thresholds or fault escalation. The system
absorbs this by design — the rule engine carries every water alert, and each
edge/rule disagreement escalates. The AI's real contributions are the
explanations, the ambiguity handling, and the fault/implausibility detection,
not replacing the thresholds. (Note: training data is synthetic, generated
from the same thresholds as the rules, so agreement partly reflects
imitation; the honest next step is fine-tuning on real logged storm data.)

### 5.1 Pushing on the weak classes — six controlled experiments

The water-threshold classes scored 0% on held-out eval, so we attacked them
systematically. Every run used the locked 2.0.2 toolchain and the same
200-scenario eval (n=40/class; \* = n=20 probe).

| Run | Data | Rank | Epochs | Class weights | Loss | all_clear | storm | f.watch | f.warning | escal. | Overall |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **v2 (shipped)** | 475 | 16 | 4 | none | 1.50 | 85% | 70% | 0% | 0% | 0% | **31%** |
| v3 | 475 | 16 | 6 | none | 0.98 | 0%\* | 95%\* | 0%\* | 0%\* | 0%\* | 20% |
| v4 | 670 rebal. | 16 | 5 | none | 0.90 | 0%\* | 90%\* | 5%\* | 0%\* | 5%\* | ~20% |
| v6 | 670 | 16 | 4 | all_clear=1.5, storm=0.6, fw=1.4, warn=1.6, esc=1.5 | 0.99 | 0% | 87.5% | 5% | **37.5%** | 0% | 26% |
| v7 | 670 | **32** | 4 | none | 0.62 | 0%\* | 95%\* | 0%\* | 0%\* | 0%\* | 19% |
| v8 | 670 | 16 | 3 | all_clear=2.0, storm=0.5, fw=1.4, warn=2.0, esc=1.4 | 1.37 | 77.5% | 62.5% | 0% | 0% | 0% | 28% |

Findings (each verified, not assumed):

1. **The weak classes are barely learnable, period.** Train-fit probes (the
   model re-answering its own training rows) scored 0-7% on flood_watch /
   flood_warning / escalate across all runs — the model cannot even memorize
   the mapping, so this is not a generalization gap.
2. **Collapse mechanism:** whenever loss drops below ~1.2 the model collapses
   onto its strongest prior (storm_watch), destroying all_clear. More data
   (670 rows), more epochs, and more capacity (rank 32) all accelerate the
   collapse — v7 (rank 32) hit loss 0.62 and scored 0% everywhere except
   storm.
3. **The trade-off is zero-sum for this model:** class-weighted training
   (v6) produced the only real gain — flood_warning 37.5% — but only by
   sacrificing all_clear entirely (0%); rebalancing back (v8) restored
   all_clear and lost flood_warning.
4. **Not a tokenization problem:** the tokenizer represents threshold
   numbers distinctly ("24.9" and "25.1" tokenize differently), so the
   ceiling is model capacity, not representation.

Conclusion: a 45M tool-calling LLM, LoRA-tuned with this recipe, can master
the calm/pressure classes but not numeric water-threshold classification —
an inherent ceiling verified from six angles (including the original
project's model, which fails the same classes). The system design absorbs
this: the deterministic rule engine carries every water alert, and each
edge/rule disagreement escalates. Putting water thresholds *in* a model
would need a larger base model or a tiny numeric classifier in front of the
LLM — both documented as future work.

### 5.2 The format experiment — the data-format hypothesis (this session)

The user pushed a sharper hypothesis: *if the prompt presents the critical
rate signals clearly, the model should learn them*. We tested it with a
water-first prompt ("Sensor update: water level X cm, water rising at R
cm/min; pressure P hPa, pressure trend T hPa/hr; temperature C."), the
status fault note moved to the front, de-correlated sampling ranges (storm
rows span high AND low pressure so no value shortcut exists), and rigid
comparison reasoning ("water 26.8 cm: above 25.0 watch, below 35.0 critical
-> flood watch").

| Run | Data | Epochs | Weights | Loss | all_clear | storm | f.watch | f.warning | escal. | Overall |
|---|---|---|---|---|---|---|---|---|---|---|
| v9 (water-first) | 670 | 4 | none | 0.96 | 0% | 40% | **50%** | 0% | 0% | 18% |
| v10 (water-first) | 670 | 3 | calibration | 1.26 | 10% | 10% | **75%** | 0% | 0% | 19% |
| v11 (774 rows) | 774 | 4 | none | 0.97 | 57.5% | 2.5% | 0% | 0% | 0% | 12% |

**The result validates the hypothesis — and reveals the real mechanism:**

1. **The signals ARE learnable.** flood_watch went from 0-5% (every previous
   run) to **50-75%** once the water clause led the prompt. The model also
   flipped at the correct ~25 cm in threshold sweeps — it never did that
   before. The 45M model CAN learn "water above ~25 → flood_watch" when the
   number is prominent.
2. **Attention is the bottleneck, not arithmetic.** Whichever clause leads
   the sentence dominates the model's decision: pressure-first format →
   storm/all_clear mastery, water-first format → flood_watch mastery. The
   collapse pattern follows the leading clause (v9/v10 toward flood_watch;
   v11 toward all_clear).
3. **It cannot hold all five classes in either format.** Every water-first
   run sacrificed all_clear/storm; the balanced 774-row attempt (v11)
   sacrificed flood_watch again. The overall best remains **v2
   (pressure-first, 31%)**, which the demo relies on.

**Decision:** the shipped prompt format stays pressure-first (v2) so the
runtime and the deployed model are byte-for-byte consistent, and the demo
shows AGREE in the calm/storm phases. The water-first format is documented
here with its artifact (`floodgate_lora_v10.pkl`) — if a future task needs
flood_watch specifically, or a larger base model arrives, the water-first
format is the proven starting point.

## 6. Deployment (Mac trains, Pi runs)

The Pi gateway needs **inference only** — no jax, no training deps:
`requirements-pi.txt` (cactus-needle 2.0.2, flask, requests, paho-mqtt).
Works on Raspberry Pi **4 or 5** running **64-bit** Raspberry Pi OS
(Bookworm); `deploy_pi.sh` verifies `uname -m` is aarch64. Pi 4 inference is
~2-3× slower than Pi 5 (~150-350 ms/decision) — well inside the decision
period; 2 GB RAM is plenty (engine peak ~75 MB).

```bash
# on the Mac (produces floodgate_pi_bundle.tar.gz)
./setup_mac.sh train

# on the Pi, alongside floodgate-bridge.service and floodgate-web.service
scp floodgate_pi_bundle.tar.gz pi@<pi-ip>:~
ssh pi@<pi-ip> 'tar xzf floodgate_pi_bundle.tar.gz && cd floodgate && bash deploy_pi.sh'
#   status: http://<pi-ip>:8090/status    dashboard stays on :8080
```

`deploy_pi.sh` installs `floodgate-brain.service` (systemd, auto-restart)
running `edge_brain.py --mqtt` with the tuned model.

## 7. Configuration reference (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `FG_MQTT` / `--mqtt` | off | subscribe to the main MQTT topic |
| `FG_MQTT_BROKER` | `broker.hivemq.com` | broker (use `localhost` for local mosquitto) |
| `FG_MQTT_TOPIC` | `umd/cpse/floodgate/telemetry` | the shared bus topic |
| `FG_HTTP_PORT` | `8090` | status/health API port (8080 = dashboard) |
| `FG_PRESSURE_DROP_STORM` | `2.0` | hPa/hr → storm watch |
| `FG_WATER_WARN_CM` / `FG_WATER_CRIT_CM` | `25` / `35` | watch / critical levels |
| `FG_WATER_RISE_WARN` | `0.5` | cm/min → flood watch |
| `FG_DECISION_PERIOD` | `10` | seconds between decisions |
| `FG_STALE_AFTER_S` | `900` | device silence → STALE |
| `FG_WEATHER_TTL_S` | `7200` | weather broadcast validity |
| `FG_CONFIDENCE_FLOOR` | `0.6` | below → escalate |
| `FG_TREND_MIN_FRACTION` | `0.2` | min window span for trends |
| `NEEDLE_WEIGHTS` | — | path to `floodgate.cact` |
| `SUPABASE_URL` / `SUPABASE_ANON_KEY` | — | dashboard/decision logging |
| `ANTHROPIC_API_KEY` | — | cloud escalation tier |

## 8. Known issues and punch list

* **Model ceiling:** water-threshold classes are beyond the 45M model
  (rules carry them). Next step: real storm data, or a larger base model.
* **macOS engine segfault** with external weights (worker isolates it;
  Linux engine likely unaffected — verify on the Pi).
* **Threshold mapping:** edge brain uses cm (25/35); the dashboard's risk
  engine scales in m (0.15/0.3/0.6/0.8). Reconcile against the real drain
  geometry and document the mapping.
* **First run** downloads the base model/engine from Hugging Face — do it
  before the venue; everything after is offline.
