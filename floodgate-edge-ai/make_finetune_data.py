#!/usr/bin/env python3
"""
make_finetune_data.py — generate the LoRA fine-tuning dataset for Needle 2.

Format is the EXACT JSONL schema the Needle 2 trainer parses (verified against
needle 2.0.x source, model/finetune.py -> render_example):

    {"query": "...",              # user text (rows without "query" are SKIPPED)
     "tools": [...],              # tool schemas available for this example
     "answers": [{"name": ..., "arguments": {...}}],   # expected call(s)
     "reasoning": "...",          # short thinking trace (model trains on it)
     "system": "..."}             # system prompt

KEY DESIGN DECISION — single source of truth:
Every query string is built by fg_core.build_prompt() and every label is
derived from fg_core.rule_engine(), the SAME functions the live runtime and
the eval harness use. The tutorial's "byte-for-byte identical" rule is now
enforced structurally instead of by careful copy-paste. (The only deliberate
exceptions are the "adversarial" escalate rows — frozen/conflicting/spiking
readings that fool the rules — where the model is trained to escalate even
though rule_engine says otherwise. Those are flagged `"adversarial": true`.)

Dataset (~475 examples):
  120  report_all_clear        normal conditions
   90  issue_storm_watch       pressure dropping >= 2 hPa/hr
   20  issue_storm_watch       OpenWeather "high risk" broadcast (weather-driven)
   90  issue_flood_watch       water elevated / rising fast
   70  issue_flood_warning     water >= critical
   40  escalate_to_cloud       adversarial: frozen / conflicting / spiking sensors
   30  escalate_to_cloud       sensor status ADC_ERROR / STALE (hardware faults)
   15  near-threshold          boundary cases, labeled by rule_engine itself

Usage:
  python3 make_finetune_data.py                 # writes finetune_data.jsonl
  python3 make_finetune_data.py --check         # + verify rows against fg_core
"""
import argparse
import json
import random

# Fixed seed => the dataset is deterministic and reproducible. (The shipped
# adapter was trained on a pre-seed generation of this exact generator —
# identical structure, counts and distributions; only the sampled values
# differ. Retraining on the seeded dataset reproduces the same loss/accuracy
# ballpark. See DESIGN.md §11.4 for the honest note.)
random.seed(int(__import__("os").environ.get("FG_DATA_SEED", "42")))

from fg_core import (PRESSURE_DROP_STORM, WATER_WARN_CM, WATER_CRIT_CM,
                     WATER_RISE_WARN, SYSTEM, rule_engine, build_prompt)

# Tool schemas — same shapes needle.build_schema() produces for the functions
# in edge_brain.py (see --check for an automatic diff against the live tools).
TOOLS = [
    {"name": "report_all_clear",
     "description": "Report that conditions are normal: stable pressure and safe water level. summary is one short sentence describing current conditions.",
     "parameters": {"type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"]}},
    {"name": "issue_storm_watch",
     "description": "Issue a storm watch because barometric pressure is falling fast, which precedes storms. Include the observed pressure trend.",
     "parameters": {"type": "object",
                    "properties": {"summary": {"type": "string"},
                                   "pressure_trend_hpa_per_hr": {"type": "number"}},
                    "required": ["summary", "pressure_trend_hpa_per_hr"]}},
    {"name": "issue_flood_watch",
     "description": "Issue a flood watch because water level is elevated or rising quickly toward the overflow threshold.",
     "parameters": {"type": "object",
                    "properties": {"summary": {"type": "string"},
                                   "water_level_cm": {"type": "number"},
                                   "water_rise_cm_per_min": {"type": "number"}},
                    "required": ["summary", "water_level_cm", "water_rise_cm_per_min"]}},
    {"name": "issue_flood_warning",
     "description": "Issue an urgent flood warning because water level indicates overflow is imminent. This is the highest severity action.",
     "parameters": {"type": "object",
                    "properties": {"summary": {"type": "string"},
                                   "water_level_cm": {"type": "number"}},
                    "required": ["summary", "water_level_cm"]}},
    {"name": "escalate_to_cloud",
     "description": "Escalate to the cloud AI for deeper analysis when the situation is ambiguous, sensor readings conflict, or confidence is low.",
     "parameters": {"type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": ["reason"]}},
]

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def r(a, b, nd=1):
    """random float in [a,b] rounded to nd decimals — same rounding the live
    feature extractor applies, so prompts match byte-for-byte."""
    return round(random.uniform(a, b), nd)


def feats(p, trend, t, w, rise, status="OK", weather=0):
    return {"temperature_c": t, "pressure_hpa": p,
            "pressure_trend_hpa_per_hr": trend, "water_level_cm": w,
            "water_rise_cm_per_min": rise, "sensor_status": status,
            "weather_high_risk": weather, "device_id": "ESP32-FloodGate",
            "samples_in_window": 99}


def row(f, decision, reasoning, summary, extra_args=None, adversarial=False):
    """Build one JSONL row from a features dict + a decision. The query comes
    from fg_core.build_prompt(f) — the exact runtime prompt."""
    arguments = {"summary": summary}
    if extra_args:
        arguments.update(extra_args)
    return {"query": build_prompt(f), "tools": TOOLS, "system": SYSTEM,
            "reasoning": reasoning, "answers": [{"name": decision,
                                                 "arguments": arguments}],
            "adversarial": adversarial, "_features": f}


# ----------------------------------------------------------------------------
# Dataset
# ----------------------------------------------------------------------------
rows = []

# 1) All clear — 120
for _ in range(120):
    f = feats(p=r(1005, 1025), trend=r(-1.2, 1.2, 2), t=r(15, 32),
              w=r(5, WATER_WARN_CM - 5), rise=r(-0.1, 0.2, 2))
    assert rule_engine(f) == "report_all_clear"
    rows.append(row(f, "report_all_clear",
        f"Pressure trend {f['pressure_trend_hpa_per_hr']} hPa/hr is within "
        f"+/-{PRESSURE_DROP_STORM}, water {f['water_level_cm']} cm is below "
        f"{WATER_WARN_CM} cm and rise {f['water_rise_cm_per_min']} cm/min is "
        f"below {WATER_RISE_WARN}. Sensor OK. Normal.",
        "Pressure stable and water level safe; conditions normal."))

# 2) Storm watch by pressure trend — 90
for _ in range(90):
    f = feats(p=r(990, 1012), trend=r(-8.0, -PRESSURE_DROP_STORM, 2), t=r(15, 32),
              w=r(5, WATER_WARN_CM - 5), rise=r(-0.1, 0.2, 2))
    assert rule_engine(f) == "issue_storm_watch"
    rows.append(row(f, "issue_storm_watch",
        f"Pressure falling at {f['pressure_trend_hpa_per_hr']} hPa/hr, beyond "
        f"the -{PRESSURE_DROP_STORM} threshold, while water is still safe. "
        "Storm watch.",
        "Barometric pressure falling rapidly; storm likely approaching.",
        {"pressure_trend_hpa_per_hr": f["pressure_trend_hpa_per_hr"]}))

# 3) Storm watch driven by the OpenWeather "high risk" broadcast — 20
#    (new in the aligned system: the main backend's floodgatebridgeandweather.py
#     publishes this flag on the same MQTT topic; a storm watch is the analog
#     of the firmware shortening its sleep cadence on the same signal.)
for _ in range(20):
    f = feats(p=r(1005, 1020), trend=r(-1.8, -0.2, 2), t=r(15, 32),
              w=r(5, WATER_WARN_CM - 5), rise=r(-0.1, 0.2, 2), weather=1)
    assert rule_engine(f) == "issue_storm_watch"
    rows.append(row(f, "issue_storm_watch",
        f"Pressure is only {f['pressure_trend_hpa_per_hr']} hPa/hr but the "
        "OpenWeather forecast flags high risk, so a storm is expected. Water "
        "is still safe. Storm watch.",
        "OpenWeather high-risk forecast; storm likely approaching.",
        {"pressure_trend_hpa_per_hr": f["pressure_trend_hpa_per_hr"]}))

# 4) Flood watch — 90 (two flavors: level-based, rise-based)
for _ in range(90):
    t = r(15, 32)
    if random.random() < 0.5:
        f = feats(p=r(995, 1020), trend=r(-3.0, 0.5, 2), t=t,
                  w=r(WATER_WARN_CM, WATER_CRIT_CM - 1), rise=r(0.0, 0.4, 2))
        why = (f"Water {f['water_level_cm']} cm exceeds the {WATER_WARN_CM} cm "
               f"watch level but is below {WATER_CRIT_CM} cm.")
    else:
        f = feats(p=r(995, 1020), trend=r(-3.0, 0.5, 2), t=t,
                  w=r(10, WATER_WARN_CM - 1), rise=r(WATER_RISE_WARN, 2.0, 2))
        why = (f"Water rising at {f['water_rise_cm_per_min']} cm/min, above "
               f"the {WATER_RISE_WARN} cm/min watch rate.")
    assert rule_engine(f) == "issue_flood_watch"
    rows.append(row(f, "issue_flood_watch", why + " Flood watch.",
        "Water level elevated or rising quickly toward overflow threshold.",
        {"water_level_cm": f["water_level_cm"],
         "water_rise_cm_per_min": f["water_rise_cm_per_min"]}))

# 5) Flood warning — 70
for _ in range(70):
    f = feats(p=r(990, 1015), trend=r(-5.0, 0.5, 2), t=r(15, 32),
              w=r(WATER_CRIT_CM, WATER_CRIT_CM + 20), rise=r(0.2, 3.0, 2))
    assert rule_engine(f) == "issue_flood_warning"
    rows.append(row(f, "issue_flood_warning",
        f"Water {f['water_level_cm']} cm is at or above the {WATER_CRIT_CM} cm "
        "critical level. Overflow imminent; highest severity.",
        "Water level critical; overflow imminent.",
        {"water_level_cm": f["water_level_cm"]}))

# 6) Adversarial escalate — 40 (deliberately contradict rule_engine: the model
#    is the one that catches what the rules can't — see TUTORIAL section 4)
for _ in range(40):
    case = random.choice(["frozen", "conflict", "spike"])
    if case == "frozen":
        f = feats(p=r(1000, 1015), trend=0.0, t=r(15, 32), w=r(10, 20), rise=0.0)
        reason = "Sensor readings frozen; possible sensor fault."
        think = ("Identical readings for 30 minutes suggests a stuck sensor, "
                 "not real conditions.")
    elif case == "conflict":
        f = feats(p=r(995, 1005), trend=r(-6.0, -3.0, 2), t=r(15, 32),
                  w=r(5, 12), rise=r(-1.5, -0.5, 2))
        reason = ("Pressure indicates storm but water level dropping sharply; "
                  "conflicting signals.")
        think = ("Storm-level pressure drop but water falling fast is "
                 "contradictory.")
    else:  # spike: physically implausible jump
        f = feats(p=r(1005, 1015), trend=r(-0.5, 0.5, 2), t=r(15, 32),
                  w=r(60, 90), rise=r(8.0, 15.0, 2))
        reason = ("Implausible instantaneous jump in water level; verify "
                  "sensor before alerting.")
        think = ("A jump from 10 cm to over 60 cm in 20 seconds is physically "
                 "implausible.")
    rows.append(row(f, "escalate_to_cloud", think + " Escalate for verification.",
                    "Escalate for verification.", {"reason": reason},
                    adversarial=True))

# 7) Sensor-status faults — 30 (new in the aligned system: the main firmware
#    reports status="ADC_ERROR" when the ADS1115/transducer is unreadable;
#    STALE means the device has gone quiet. Both are hardware-fault escalations.)
for _ in range(30):
    status = random.choice(["ADC_ERROR"] * 2 + ["STALE"])
    f = feats(p=r(1000, 1018), trend=r(-1.5, 1.5, 2), t=r(15, 32),
              w=r(8, 30), rise=r(-0.2, 0.3, 2), status=status)
    assert rule_engine(f) == "escalate_to_cloud"
    rows.append(row(f, "escalate_to_cloud",
        f"Sensor status is {status}: water readings cannot be trusted. "
        "Escalate for maintenance/verification rather than alerting on bad data.",
        "Escalate for verification.", {"reason": f"Sensor status {status}; "
                                                  "water data not trustworthy."}))

# 8) Near-threshold boundary cases — 15 (labeled by rule_engine itself, so the
#    model learns the exact same crisp boundaries the safety net enforces)
for _ in range(15):
    kind = random.choice(["water_level", "rise_rate", "pressure_trend"])
    if kind == "water_level":
        f = feats(p=r(1005, 1018), trend=r(-1.5, 0.5, 2), t=r(15, 32),
                  w=r(WATER_WARN_CM - 1.5, WATER_WARN_CM + 1.5), rise=r(0.0, 0.3, 2))
    elif kind == "rise_rate":
        f = feats(p=r(1005, 1018), trend=r(-1.5, 0.5, 2), t=r(15, 32),
                  w=r(10, 22), rise=r(WATER_RISE_WARN - 0.2, WATER_RISE_WARN + 0.2))
    else:
        f = feats(p=r(1000, 1015), trend=r(-PRESSURE_DROP_STORM - 0.5,
                                           -PRESSURE_DROP_STORM + 0.5), t=r(15, 32),
                  w=r(5, 20), rise=r(0.0, 0.3, 2))
    dec = rule_engine(f)
    rows.append(row(f, dec,
        f"Boundary case near the rule threshold; rule engine says {dec.replace('_', ' ')}.",
        f"Rule-aligned boundary decision: {dec.replace('_', ' ')}."))

random.shuffle(rows)
with open("finetune_data.jsonl", "w") as fh:
    for x in rows:
        fh.write(json.dumps(x) + "\n")

counts = {}
for x in rows:
    counts[x["answers"][0]["name"]] = counts.get(x["answers"][0]["name"], 0) + 1
print(f"Wrote {len(rows)} examples to finetune_data.jsonl")
print("  distribution:", dict(sorted(counts.items())))


def check():
    """Verify every row is self-consistent with fg_core, and that the JSONL
    'tools' schemas match what needle.build_schema() produces at runtime."""
    import needle
    import edge_brain as E
    live = {s["name"]: s for s in (needle.build_schema(t) for t in E.TOOLS)}
    for t in TOOLS:
        assert live[t["name"]] == t, f"tools schema drift: {t['name']}"
    print(f"  tools schemas match edge_brain live tools ({len(TOOLS)} tools)")
    n_bad = 0
    for x in rows:
        f = x["_features"]
        if x["adversarial"]:
            continue  # deliberate disagreement with rules
        dec = rule_engine(f)
        if dec != x["answers"][0]["name"]:
            n_bad += 1
            print(f"  MISMATCH: rule={dec} answer={x['answers'][0]['name']} "
                  f"query={x['query'][:80]}...")
    print(f"  rule-consistency: {len(rows) - n_bad}/{len(rows)} rows agree "
          f"(adversarial rows excluded from this check)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify dataset vs fg_core + live tools")
    args = ap.parse_args()
    if args.check:
        check()
