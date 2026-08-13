#!/usr/bin/env python3
"""
FloodGate Edge Brain — on-device AI decision layer for the FloodGate project.

Runs on a Raspberry Pi 5. Receives sensor readings from the ESP32 — BMP280
(temp/pressure) + DFRobot KIT0139 water-level transducer via ADS1115 — in the
EXACT payload schema the main FloodGate project uses
(floodgate_firmware.ino -> MQTT -> floodgatebridgeandweather.py -> Supabase):

    {"device_id": "ESP32-FloodGate",
     "water_depth": 0.245,          # METERS (frontend charts "Depth (m)")
     "atm_pressure_hpa": 1013.2,    # hPa
     "ambient_temp_c": 24.1,        # deg C
     "status": "OK"}                # "OK" | "ADC_ERROR"

Two ingestion paths, both aligned with the main project:

  1. MQTT (recommended, --mqtt / FG_MQTT=1): subscribe to the SAME topic the
     main firmware publishes to (default umd/cpse/floodgate/telemetry). The
     existing ESP32 firmware works unchanged — no reflash needed. The OpenWeather
     forecast broadcasts from floodgatebridgeandweather.py are consumed here too
     (a "high risk" flag feeds the storm-watch decision).
  2. HTTP (default): POST http://<pi-ip>:8080/reading with the same JSON; the
     updated esp32_sender.ino in this repo does exactly that. The legacy
     {"temperature_c","pressure_hpa","water_level_cm"} schema is still accepted.

The brain computes trends, then asks a locally-running Needle 2 LLM (14 MB,
no network needed for inference) to decide which action to take via tool
calling. A deterministic rule engine (fg_core.rule_engine) runs in parallel as
a safety net and ALWAYS drives the real alert; ambiguous / low-confidence /
faulty-sensor situations escalate to the cloud (Anthropic API) — the
"edge-cloud collaboration" story.

Architecture:

  ESP32 (main firmware) ──MQTT──▶ broker.hivemq.com ──subscribe──▶ Pi 5 (this script)
  esp32_sender.ino      ──HTTP POST /reading──▶ Pi 5 (this script)     │
  floodgatebridgeandweather.py ──MQTT broadcast──▶ Pi 5 (this script)   │
                                     ├─ fg_core.SensorWindow (rolling window + features)
                                     ├─ fg_core.rule_engine (deterministic, always trusted)
                                     ├─ Needle 2  (local LLM tool calling)
                                     ├─ compare + log agreement
                                     └─ escalate_to_cloud (optional, only when unsure)
                                           └─▶ Supabase (existing dashboard)

Run:
  pip install -r requirements-pi.txt          # inference-only, no jax needed
  python3 edge_brain.py                # HTTP status API on :8090
  python3 edge_brain.py --mqtt         # also subscribe to the main MQTT topic
  python3 edge_brain.py --simulate     # no ESP32 needed; runs the 4-phase demo

The HTTP API lives on port 8090 by default (FG_HTTP_PORT to override) so it
never collides with the main FloodGate web dashboard on :8080.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import threading

import needle  # pip install cactus-needle

from fg_core import (Reading, SensorWindow, parse_payload, set_weather,
                     rule_engine, build_prompt, SYSTEM, DECISION_PERIOD_S,
                     CONFIDENCE_FLOOR, MQTT_BROKER, MQTT_PORT, MQTT_TOPIC,
                     HTTP_PORT)

# ----------------------------------------------------------------------------
# Optional integrations
# ----------------------------------------------------------------------------
SUPABASE_URL   = os.environ.get("SUPABASE_URL", "")                       # e.g. https://xyz.supabase.co
SUPABASE_KEY   = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "edge_decisions")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")               # optional cloud escalation

WINDOW = SensorWindow()

# ----------------------------------------------------------------------------
# Actions — these are the tools Needle can call. The docstrings and type
# hints ARE the schema (needle.build_schema reads them), so keep them clear.
# They are mirrored in make_finetune_data.py's JSONL "tools" field.
# ----------------------------------------------------------------------------
LAST_DECISION = {"needle": None, "rule": None, "agreed": None, "detail": None}


def _announce(kind: str, message: str, severity: str):
    line = f"[{time.strftime('%H:%M:%S')}] {severity.upper():8s} {kind}: {message}"
    print("\033[93m" + line + "\033[0m" if severity != "info" else line)
    push_supabase({"kind": kind, "message": message, "severity": severity})
    return {"status": "announced", "kind": kind}


@needle.tool
def report_all_clear(summary: str):
    """Report that conditions are normal: stable pressure and safe water level. summary is one short sentence describing current conditions."""
    return _announce("all_clear", summary, "info")


@needle.tool
def issue_storm_watch(summary: str, pressure_trend_hpa_per_hr: float):
    """Issue a storm watch because barometric pressure is falling fast, which precedes storms. Include the observed pressure trend."""
    return _announce("storm_watch", f"{summary} (trend {pressure_trend_hpa_per_hr} hPa/hr)", "warning")


@needle.tool
def issue_flood_watch(summary: str, water_level_cm: float, water_rise_cm_per_min: float):
    """Issue a flood watch because water level is elevated or rising quickly toward the overflow threshold."""
    return _announce("flood_watch", f"{summary} (level {water_level_cm} cm, rising {water_rise_cm_per_min} cm/min)", "warning")


@needle.tool
def issue_flood_warning(summary: str, water_level_cm: float):
    """Issue an urgent flood warning because water level indicates overflow is imminent. This is the highest severity action."""
    return _announce("flood_warning", f"{summary} (level {water_level_cm} cm)", "critical")


@needle.tool
def escalate_to_cloud(reason: str):
    """Escalate to the cloud AI for deeper analysis when the situation is ambiguous, sensor readings conflict, or confidence is low."""
    return {"status": "escalation_requested", "reason": reason}


TOOLS = [report_all_clear, issue_storm_watch, issue_flood_watch, issue_flood_warning, escalate_to_cloud]


# ----------------------------------------------------------------------------
# Inference isolation (war story — see TUTORIAL.md §9)
#
# The needle engine dylibs on macOS segfault after ~6-8 inferences when
# external .cact weights are loaded (verified on engines 2.0.0 and 2.0.3; the
# baked-in base-model path is unaffected). A segfault would kill the whole
# process, so inference runs in a supervised child process (infer_worker.py):
# if the worker dies mid-cycle, the client restarts it and retries the prompt.
# The rule engine keeps driving alerts regardless — the AI is never a single
# point of failure, by design.
# ----------------------------------------------------------------------------
class InferenceClient:
    def __init__(self, weights=None):
        self.weights = weights
        self.proc = None

    def _start(self):
        cmd = [sys.executable, "-u",
               os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "infer_worker.py")]
        if self.weights:
            cmd += ["--weights", self.weights]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     bufsize=1)

    def kill(self):
        if self.proc is not None:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def complete(self, prompt: str, retries: int = 2, timeout_s: float = 30.0) -> dict:
        """Run one inference through the supervised worker. Raises if the
        worker cannot produce a response after retries or within timeout_s."""
        import select
        last_err = None
        for attempt in range(retries + 1):
            if self.proc is None or self.proc.poll() is not None:
                self._start()
            try:
                self.proc.stdin.write(json.dumps({"prompt": prompt}) + "\n")
                self.proc.stdin.flush()
                ready, _, _ = select.select([self.proc.stdout], [], [], timeout_s)
                if not ready:
                    raise TimeoutError(f"inference worker timed out after {timeout_s}s")
                out = self.proc.stdout.readline()
                if not out:
                    raise RuntimeError("inference worker died (no output)")
                msg = json.loads(out)
                if not msg.get("ok"):
                    raise RuntimeError(msg.get("error", "worker error"))
                return msg["raw"]
            except Exception as e:
                last_err = e
                self.kill()
        raise RuntimeError(f"inference failed after retries: {last_err}")


def push_supabase(row: dict):
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    try:
        import requests
        requests.post(
            f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
            headers={"apikey": SUPABASE_KEY,
                     "Authorization": f"Bearer {SUPABASE_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "return=minimal"},
            json={**row, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            timeout=5,
        )
    except Exception as e:
        print(f"  (supabase push failed: {e})")


def cloud_escalation(features: dict, edge_decision: str, reason: str) -> str:
    """Edge-cloud collaboration: only called when the edge model is unsure.
    Uses the Anthropic API to produce a deeper analysis + explanation."""
    if not ANTHROPIC_API_KEY:
        return "(cloud escalation skipped: no ANTHROPIC_API_KEY set)"
    try:
        import requests
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001",
                  "max_tokens": 300,
                  "messages": [{"role": "user", "content":
                      "You are the cloud tier of a flood-detection system. The edge "
                      "model was unsure. Sensor features: " + json.dumps(features) +
                      f". Edge tentative decision: {edge_decision}. Reason for "
                      f"escalation: {reason}. In 2-3 sentences, give your assessment "
                      "and the single recommended action."}]},
            timeout=20,
        )
        data = resp.json()
        text = " ".join(b.get("text", "") for b in data.get("content", []))
        print(f"  \033[96m[CLOUD] {text}\033[0m")
        push_supabase({"kind": "cloud_analysis", "message": text, "severity": "info"})
        return text
    except Exception as e:
        return f"(cloud escalation failed: {e})"


# ----------------------------------------------------------------------------
# The decision loop
# ----------------------------------------------------------------------------
def _drive_rule_alert(f, rule_decision: str):
    """The rule engine drives the REAL alert (safety net). Call its tool with
    exactly the arguments that tool's signature declares."""
    if rule_decision == "escalate_to_cloud":
        # Escalation has no sensor arguments — build its reason from state.
        escalate_to_cloud(reason=f"Rule engine: sensor status {f['sensor_status']}; "
                                 "verify sensor before trusting water readings.")
        return
    for fn in TOOLS:
        if fn.__name__ == rule_decision:
            args = {"summary": f"Rule engine: {rule_decision.replace('_', ' ')}"}
            if "pressure_trend_hpa_per_hr" in fn.__code__.co_varnames:
                args["pressure_trend_hpa_per_hr"] = f["pressure_trend_hpa_per_hr"]
            if "water_level_cm" in fn.__code__.co_varnames:
                args["water_level_cm"] = f["water_level_cm"]
            if "water_rise_cm_per_min" in fn.__code__.co_varnames:
                args["water_rise_cm_per_min"] = f["water_rise_cm_per_min"]
            fn(**args)
            return


def decision_loop(client: "InferenceClient"):
    while True:
        time.sleep(DECISION_PERIOD_S)
        f = WINDOW.features()
        if f is None or f["samples_in_window"] < 3:
            continue

        prompt = build_prompt(f)
        rule_decision = rule_engine(f)

        t0 = time.perf_counter()
        try:
            response = client.complete(prompt)
        except Exception as e:
            print(f"  (needle inference failed: {e}; rule engine only this cycle)")
            response = {}
        latency_ms = (time.perf_counter() - t0) * 1000

        calls = response.get("function_calls") or []
        needle_decision = calls[0]["name"] if calls else "no_call"
        confidence = response.get("confidence")

        agreed = (needle_decision == rule_decision)
        LAST_DECISION.update({"needle": needle_decision, "rule": rule_decision,
                              "agreed": agreed, "detail": f})

        badge = "\033[92mAGREE\033[0m" if agreed else "\033[91mDISAGREE\033[0m"
        conf_s = f" conf={confidence:.2f}" if isinstance(confidence, (int, float)) else ""
        print(f"[{time.strftime('%H:%M:%S')}] edge={needle_decision:20s} "
              f"rule={rule_decision:20s} {badge}  {latency_ms:.0f}ms{conf_s}",
              flush=True)

        _drive_rule_alert(f, rule_decision)

        # Edge-cloud collaboration: escalate on low confidence, disagreement,
        # an explicit escalate_to_cloud call, or a faulty/stale sensor.
        should_escalate = (
            needle_decision == "escalate_to_cloud"
            or not agreed
            or f["sensor_status"] != "OK"
            or (isinstance(confidence, (int, float)) and confidence < CONFIDENCE_FLOOR)
        )
        if should_escalate:
            if f["sensor_status"] != "OK":
                reason = f"sensor status {f['sensor_status']}"
            elif needle_decision == "escalate_to_cloud":
                reason = "edge model requested escalation"
            elif not agreed:
                reason = "edge/rule disagreement"
            else:
                reason = f"low confidence {confidence}"
            cloud_escalation(f, needle_decision, reason)

        push_supabase({"kind": "decision",
                       "message": json.dumps({"needle": needle_decision,
                                              "rule": rule_decision,
                                              "agreed": agreed,
                                              "latency_ms": round(latency_ms),
                                              "features": f}),
                       "severity": "info"})


# ----------------------------------------------------------------------------
# Ingestion: HTTP (Flask) and MQTT (paho) — both parse via fg_core.parse_payload
# ----------------------------------------------------------------------------
def _ingest(d: dict) -> str:
    """Route one parsed JSON payload. Returns a short description for logs."""
    kind = parse_payload(d)
    if kind is None:
        return "unrecognized"
    if kind[0] == "weather":
        set_weather(kind[1])
        return "weather(broadcast)"
    WINDOW.add(kind[1])
    return "reading"


def start_http_server():
    from flask import Flask, request, jsonify
    app = Flask(__name__)

    @app.post("/reading")
    def reading():
        try:
            d = request.get_json(force=True, silent=True)
        except Exception:
            d = None
        if d is None or not isinstance(d, dict):
            return jsonify({"ok": False, "error": "expected a JSON object"}), 400
        kind = _ingest(d)
        return jsonify({"ok": True, "schema": kind})

    @app.get("/status")
    def status():
        return jsonify({"features": WINDOW.features(), "last_decision": LAST_DECISION})

    @app.get("/health")
    def health():
        return jsonify({"ok": True, "mqtt": mqtt_running()})

    app.run(host="0.0.0.0", port=HTTP_PORT)


_MQTT_RUNNING = {"state": False}


def mqtt_running() -> bool:
    return bool(_MQTT_RUNNING["state"])


def start_mqtt_listener():
    """Subscribe to the main project's MQTT topic. The ESP32 firmware's
    telemetry AND the OpenWeather bridge's broadcasts both arrive here, so
    no firmware changes are required to wire the edge brain into the main
    FloodGate stack."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("  (paho-mqtt not installed — MQTT ingestion disabled. "
              "pip install paho-mqtt)")
        return

    def on_message(client, userdata, msg):
        try:
            d = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            print(f"  (mqtt: non-JSON payload from {msg.topic})")
            return
        kind = _ingest(d)
        if kind != "unrecognized":
            print(f"[MQTT:{msg.topic}] {kind}: {json.dumps(d)}", flush=True)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    try:
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.subscribe(MQTT_TOPIC)
        _MQTT_RUNNING["state"] = True
        print(f"MQTT connected: {MQTT_BROKER}:{MQTT_PORT} topic '{MQTT_TOPIC}'")
        client.loop_forever()
    except Exception as e:
        print(f"  (mqtt connect failed: {e}; continuing HTTP-only)")


# ----------------------------------------------------------------------------
# Built-in simulator: normal -> storm (with weather broadcast) -> flood -> fault
# Feeds the SAME ingestion path, so the demo exercises the aligned schema.
# ----------------------------------------------------------------------------
def simulate():
    import math
    import random
    print("Simulator: 4 phases x ~45s (normal / storm / flood / sensor fault). "
          "Ctrl-C to stop.\n")
    # Seed ~15 min of quiet baseline history so trend features are meaningful
    # from the first decision (trends need real history — see fg_core.rate).
    t0 = time.time() - 900
    for i in range(90):
        WINDOW.add(Reading(ts=t0 + i * 10, temperature_c=24.0,
                           pressure_hpa=1013.0, water_level_cm=10.0,
                           device_id="ESP32-FloodGate", status="OK"))
    t = 0
    pressure, water = 1013.0, 10.0
    while True:
        phase = (t // 45) % 4
        status = "OK"
        if phase == 0:                     # normal
            pressure += random.uniform(-0.02, 0.02)
            water += random.uniform(-0.05, 0.05)
        elif phase == 1:                   # storm approaching: fast pressure drop
            if t % 45 == 0:                # once per phase: weather broadcast
                set_weather({"source": "openweather_api", "city": "Potomac",
                             "high risk": 1})
                print("  [SIM] OpenWeather broadcast: high risk = 1", flush=True)
            pressure -= 0.08 + random.uniform(0, 0.02)   # ~ -5 hPa/hr equivalent
            water += random.uniform(-0.02, 0.08)
        elif phase == 2:                   # flood: water rising fast
            water += 0.35 + random.uniform(0, 0.1)
            pressure += random.uniform(-0.02, 0.02)
        else:                              # sensor fault: ADC_ERROR
            status = "ADC_ERROR"
            water += random.uniform(-0.02, 0.02)   # frozen-ish water
            pressure += random.uniform(-0.02, 0.02)
        water = max(5.0, water)
        WINDOW.add(Reading(ts=time.time(), temperature_c=24 + 2 * math.sin(t / 30),
                           pressure_hpa=pressure, water_level_cm=water,
                           device_id="ESP32-FloodGate", status=status))
        t += 1
        time.sleep(1)


# ----------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true",
                    help="run built-in sensor simulator instead of HTTP server")
    ap.add_argument("--mqtt", action="store_true",
                    help="also subscribe to the main project's MQTT topic "
                         f"(default {MQTT_TOPIC}); also honors FG_MQTT=1")
    ap.add_argument("--weights", default=os.environ.get("NEEDLE_WEIGHTS") or None,
                    help="path to a fine-tuned .cact (default: base model; "
                         "also honors $NEEDLE_WEIGHTS)")
    args = ap.parse_args()

    which = args.weights or "base model"
    print(f"Loading Needle 2 (14 MB, local, offline inference) [{which}]...")
    client = InferenceClient(weights=args.weights)
    print("Model ready (inference isolated in a crash-restartable worker).\n")

    threading.Thread(target=decision_loop, args=(client,), daemon=True).start()

    if args.simulate:
        simulate()
    else:
        if args.mqtt or os.environ.get("FG_MQTT", "") == "1":
            threading.Thread(target=start_mqtt_listener, daemon=True).start()
        start_http_server()
