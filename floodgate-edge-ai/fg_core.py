#!/usr/bin/env python3
"""
fg_core.py — the single source of truth for FloodGate Edge Brain logic.

Everything that must stay byte-for-byte identical across the pipeline lives
here so it can never drift:

  * thresholds / configuration defaults     (used by rules, data, eval)
  * the sensor payload parsers              (main firmware schema + legacy)
  * SensorWindow + trend features           (rule engine AND LLM features)
  * rule_engine()                           (deterministic safety net)
  * build_prompt()                          (the exact LLM query string)

`edge_brain.py` (runtime), `make_finetune_data.py` (dataset) and
`eval_model.py` (scoring) all import from this module — there is no second
copy of any of these strings anywhere. This is the fix for the classic
"training and deployment drifted apart" failure mode documented in the
tutorial: the query format is now ONE function, not three near-copies.

This module deliberately does NOT import `needle`, so data generation and
evaluation can import it quickly and independently of the model library.
"""

import os
import time
import threading
from collections import deque
from dataclasses import dataclass

# ----------------------------------------------------------------------------
# Configuration (env-overridable). Thresholds are the SAME numbers the
# training-data generator and the eval harness use — single source of truth.
# ----------------------------------------------------------------------------
PRESSURE_DROP_STORM = float(os.environ.get("FG_PRESSURE_DROP_STORM", "2.0"))   # hPa/hr sustained drop => storm watch
WATER_WARN_CM       = float(os.environ.get("FG_WATER_WARN_CM", "25"))          # absolute level warning
WATER_CRIT_CM       = float(os.environ.get("FG_WATER_CRIT_CM", "35"))          # overflow imminent
WATER_RISE_WARN     = float(os.environ.get("FG_WATER_RISE_WARN", "0.5"))       # cm/min rise rate warning
DECISION_PERIOD_S   = float(os.environ.get("FG_DECISION_PERIOD", "10"))        # how often the brain decides
CONFIDENCE_FLOOR    = float(os.environ.get("FG_CONFIDENCE_FLOOR", "0.6"))      # below this => cloud escalation
# No reading for this long => STALE. The ESP32 deep-sleeps 300 s (5 min) at
# normal risk and 60 s under a high-risk weather broadcast, so 900 s = three
# missed normal cycles before we declare the device stale.
STALE_AFTER_S       = float(os.environ.get("FG_STALE_AFTER_S", "900"))
WEATHER_TTL_S       = float(os.environ.get("FG_WEATHER_TTL_S", "7200"))        # how long a weather broadcast stays valid
# Trends need real history: a rate computed over a window whose span is under
# this fraction of its nominal length is noise (e.g. two readings 1 s apart
# "prove" a -800000 hPa/hr collapse). Below the fraction, rate() returns 0.
TREND_MIN_FRACTION  = float(os.environ.get("FG_TREND_MIN_FRACTION", "0.2"))

MQTT_BROKER         = os.environ.get("FG_MQTT_BROKER", "broker.hivemq.com")
MQTT_PORT           = int(os.environ.get("FG_MQTT_PORT", "1883"))
# The topic the ESP32 firmware publishes telemetry to and the OpenWeather
# bridge publishes forecast broadcasts to. Subscribing to it is how the edge
# brain joins the existing FloodGate bus (same topic the main bridge uses).
MQTT_TOPIC          = os.environ.get("FG_MQTT_TOPIC", "umd/cpse/floodgate/telemetry")

# HTTP status/health API port. 8080 belongs to the main FloodGate web
# dashboard on the Pi, so the edge brain defaults to 8090 (override with
# FG_HTTP_PORT if you want it elsewhere).
HTTP_PORT           = int(os.environ.get("FG_HTTP_PORT", "8090"))

# ----------------------------------------------------------------------------
# Sensor readings
# ----------------------------------------------------------------------------
@dataclass
class Reading:
    ts: float
    temperature_c: float
    pressure_hpa: float
    water_level_cm: float
    device_id: str = "ESP32-FloodGate"
    status: str = "OK"          # "OK" | "ADC_ERROR" | (derived: "STALE")


def parse_payload(d: dict):
    """Accept one JSON payload and route it.

    Returns one of:
      ("reading", Reading)  — a sensor reading (main or legacy schema)
      ("weather", dict)     — an OpenWeather forecast broadcast
      None                  — unrecognized payload

    MAIN schema (floodgate_firmware.ino / Floodgate-main):
      {"device_id": "ESP32-FloodGate",
       "water_depth": 0.245,          # METERS (frontend charts "Depth (m)")
       "atm_pressure_hpa": 1013.2,    # hPa
       "ambient_temp_c": 24.1,        # deg C
       "status": "OK"}                # "OK" | "ADC_ERROR"

    LEGACY edge-ai schema (old esp32_sender.ino, still accepted):
      {"temperature_c": 24.1, "pressure_hpa": 1008.2, "water_level_cm": 12.5}

    WEATHER broadcast (floodgatebridgeandweather.py, same MQTT topic):
      {"source": "openweather_api", "city": ..., "high risk": 0|1, ...}
    """
    if not isinstance(d, dict):
        return None
    if d.get("source") == "openweather_api":
        return ("weather", d)
    if "water_depth" in d:
        # Main firmware schema: depth is in METERS; internal features use cm.
        return ("reading", Reading(
            ts=time.time(),
            temperature_c=float(d["ambient_temp_c"]),
            pressure_hpa=float(d["atm_pressure_hpa"]),
            water_level_cm=float(d["water_depth"]) * 100.0,
            device_id=str(d.get("device_id", "unknown")),
            status=str(d.get("status", "OK"))))
    if "water_level_cm" in d:  # legacy edge-ai schema
        return ("reading", Reading(
            ts=time.time(),
            temperature_c=float(d["temperature_c"]),
            pressure_hpa=float(d["pressure_hpa"]),
            water_level_cm=float(d["water_level_cm"]),
            device_id=str(d.get("device_id", "legacy")),
            status=str(d.get("status", "OK"))))
    return None


# Latest OpenWeather forecast broadcast (set from MQTT/HTTP, never from the
# firmware telemetry — the main backend's floodgatebridgeandweather.py emits it).
WEATHER = {"high_risk": 0, "ts": 0.0, "city": None}


def set_weather(payload: dict):
    WEATHER["high_risk"] = 1 if int(payload.get("high risk", 0) or 0) else 0
    WEATHER["ts"] = time.time()
    WEATHER["city"] = payload.get("city")


def weather_high_risk() -> int:
    if time.time() - WEATHER["ts"] <= WEATHER_TTL_S and WEATHER["high_risk"]:
        return 1
    return 0


class SensorWindow:
    """Keeps ~60 min of readings and computes the trend features that both
    the rule engine and the LLM consume. One source of truth for features."""

    def __init__(self, maxlen=720):
        self.readings = deque(maxlen=maxlen)
        self.lock = threading.Lock()

    def add(self, r: Reading):
        with self.lock:
            self.readings.append(r)

    def features(self):
        with self.lock:
            rs = list(self.readings)
        if not rs:
            return None
        now = rs[-1]

        def rate(attr, window_s):
            """Units per second over the last window_s, via oldest point in window.
            Returns 0 until the window has at least TREND_MIN_FRACTION of its
            nominal span of real data — short spans are startup noise."""
            cutoff = now.ts - window_s
            past = [r for r in rs if r.ts >= cutoff]
            if len(past) < 2:
                return 0.0
            first = past[0]
            dt = now.ts - first.ts
            if dt <= 0 or dt < window_s * TREND_MIN_FRACTION:
                return 0.0
            return (getattr(now, attr) - getattr(first, attr)) / dt

        # Sensor health: the ADC/transducer on the ESP32 reports ADC_ERROR
        # when the water sensor is unreadable; STALE means the device has
        # gone quiet (deep sleep is normal, silence for > STALE_AFTER_S is not).
        stale = (time.time() - now.ts) > STALE_AFTER_S
        status = "STALE" if stale else now.status

        return {
            "temperature_c": round(now.temperature_c, 1),
            "pressure_hpa": round(now.pressure_hpa, 1),
            "pressure_trend_hpa_per_hr": round(rate("pressure_hpa", 3600) * 3600, 2),
            "water_level_cm": round(now.water_level_cm, 1),
            "water_rise_cm_per_min": round(rate("water_level_cm", 300) * 60, 2),
            "sensor_status": status,
            "weather_high_risk": weather_high_risk(),
            "device_id": now.device_id,
            "samples_in_window": len(rs),
        }


# ----------------------------------------------------------------------------
# Deterministic rule engine — the safety net. This ALWAYS drives the real
# alert. The LLM decision is compared against it and logged. Precedence:
# sensor fault > critical water > watch-level water > storm signal > clear.
# ----------------------------------------------------------------------------
def rule_engine(f) -> str:
    if f["sensor_status"] in ("ADC_ERROR", "STALE"):
        return "escalate_to_cloud"          # never alert on garbage/stale data
    if f["water_level_cm"] >= WATER_CRIT_CM:
        return "issue_flood_warning"
    if f["water_level_cm"] >= WATER_WARN_CM or f["water_rise_cm_per_min"] >= WATER_RISE_WARN:
        return "issue_flood_watch"
    if f["pressure_trend_hpa_per_hr"] <= -PRESSURE_DROP_STORM or f["weather_high_risk"]:
        return "issue_storm_watch"
    return "report_all_clear"


# ----------------------------------------------------------------------------
# The LLM prompt — byte-for-byte identical in training data, live runtime and
# evaluation. Never write this string anywhere else; call build_prompt().
# ----------------------------------------------------------------------------
NOTE_STATUS = " Note: sensor status {status}."                     # e.g. ADC_ERROR / STALE
NOTE_WEATHER = " Note: OpenWeather forecast flags high risk."


def build_prompt(f: dict) -> str:
    """Turn a features dict into the exact query sentence the model sees.

    The field order, units and wording must match make_finetune_data.py's
    training rows — they do, because both call this one function.
    """
    q = (f"Sensor update: pressure {f['pressure_hpa']} hPa, "
         f"pressure trend {f['pressure_trend_hpa_per_hr']} hPa/hr, "
         f"temperature {f['temperature_c']} C, "
         f"water level {f['water_level_cm']} cm, "
         f"water rise rate {f['water_rise_cm_per_min']} cm/min. "
         "Decide the appropriate action.")
    notes = ""
    if f["sensor_status"] != "OK":
        notes += NOTE_STATUS.format(status=f["sensor_status"])
    if f["weather_high_risk"]:
        notes += NOTE_WEATHER
    return q + notes


# The system prompt shown to the model (same text in training and runtime).
SYSTEM = (
    "You are the on-device decision brain of FloodGate, an early flood detection "
    "device. You receive sensor summaries (barometric pressure, pressure trend, "
    "temperature, water level in a drain, water rise rate) and must call exactly "
    "one tool. Falling pressure of 2 hPa/hr or more means a storm is likely "
    f"approaching. Water above {WATER_WARN_CM} cm or rising over {WATER_RISE_WARN} "
    f"cm/min deserves a flood watch; above {WATER_CRIT_CM} cm is an urgent flood "
    "warning. If the sensor status is not OK (faulty or stale readings), if "
    "readings conflict or are physically implausible, or if the situation is "
    "otherwise unclear, escalate to cloud."
)
