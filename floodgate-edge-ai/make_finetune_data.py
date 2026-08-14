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

Dataset (~670 examples — rebalanced toward the water/fault classes, with
threshold-contrast pairs; DECORRELATED ranges so no feature-value shortcut
can satisfy the labels, and rigid comparison reasoning that teaches the
model the exact threshold procedure):
  120  report_all_clear        normal conditions (pressure 990-1025)
   90  issue_storm_watch       pressure trend <= -2 hPa/hr (pressure 995-1025)
   20  issue_storm_watch       OpenWeather "high risk" broadcast (weather-driven)
  150  issue_flood_watch       water elevated / rising fast  (75 + 75)
  120  issue_flood_warning     water >= critical
   70  escalate_to_cloud       sensor status ADC_ERROR / STALE (hardware faults)
   40  escalate_to_cloud       adversarial: frozen / conflicting / spiking sensors
   36  threshold-contrast      pairs straddling each threshold + precedence cases

Usage:
  python3 make_finetune_data.py                 # writes finetune_data.jsonl
  python3 make_finetune_data.py --check         # + verify rows against fg_core
"""
import argparse
import json
import random

# Fixed seed => the dataset is deterministic and reproducible. See DESIGN.md
# §5.1 for the honest traceability note and the full experiment log.
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

# 1) All clear — 120  (pressure spans 990-1025 so a LOW pressure value alone
#    does NOT predict all_clear — kills the pressure-value shortcut)
for _ in range(170):
    f = feats(p=r(990, 1025), trend=r(-1.2, 1.2, 2), t=r(15, 32),
              w=r(5, WATER_WARN_CM - 1), rise=r(-0.1, 0.2, 2))
    assert rule_engine(f) == "report_all_clear"
    rows.append(row(f, "report_all_clear",
        f"water {f['water_level_cm']} cm below {WATER_WARN_CM}; "
        f"rise {f['water_rise_cm_per_min']} cm/min below {WATER_RISE_WARN}; "
        f"trend {f['pressure_trend_hpa_per_hr']} within +/-{PRESSURE_DROP_STORM} -> all clear.",
        "Pressure stable and water level safe; conditions normal."))

# 2) Storm watch by pressure trend — 90  (pressure spans 995-1025, so HIGH
#    pressure + falling trend still means storm — the trend is the ONLY signal)
for _ in range(90):
    f = feats(p=r(995, 1025), trend=r(-8.0, -PRESSURE_DROP_STORM, 2), t=r(15, 32),
              w=r(5, WATER_WARN_CM - 1), rise=r(-0.1, 0.2, 2))
    assert rule_engine(f) == "issue_storm_watch"
    rows.append(row(f, "issue_storm_watch",
        f"trend {f['pressure_trend_hpa_per_hr']} hPa/hr below -{PRESSURE_DROP_STORM} "
        f"storm threshold; water {f['water_level_cm']} cm below {WATER_WARN_CM}; "
        f"rise {f['water_rise_cm_per_min']} below {WATER_RISE_WARN} -> storm watch.",
        "Barometric pressure falling rapidly; storm likely approaching.",
        {"pressure_trend_hpa_per_hr": f["pressure_trend_hpa_per_hr"]}))

# 3) Storm watch driven by the OpenWeather "high risk" broadcast — 20
#    (new in the aligned system: the main backend's floodgatebridgeandweather.py
#     publishes this flag on the same MQTT topic; a storm watch is the analog
#     of the firmware shortening its sleep cadence on the same signal.)
for _ in range(20):
    f = feats(p=r(1000, 1025), trend=r(-1.8, -0.2, 2), t=r(15, 32),
              w=r(5, WATER_WARN_CM - 5), rise=r(-0.1, 0.2, 2), weather=1)
    assert rule_engine(f) == "issue_storm_watch"
    rows.append(row(f, "issue_storm_watch",
        f"weather high-risk flag set; trend {f['pressure_trend_hpa_per_hr']} within "
        f"+/-{PRESSURE_DROP_STORM} but forecast says storm; water "
        f"{f['water_level_cm']} below {WATER_WARN_CM} -> storm watch.",
        "OpenWeather high-risk forecast; storm likely approaching.",
        {"pressure_trend_hpa_per_hr": f["pressure_trend_hpa_per_hr"]}))

# 4) Flood watch — 150 (75 level-based, 75 rise-based; pressure/trend span wide
#    ranges so the WATER numbers are the only discriminators)
for _ in range(150):
    t = r(15, 32)
    if random.random() < 0.5:
        f = feats(p=r(995, 1025), trend=r(-3.0, 0.5, 2), t=t,
                  w=r(WATER_WARN_CM, WATER_CRIT_CM - 1), rise=r(0.0, 0.4, 2))
        why = (f"water {f['water_level_cm']} cm: above {WATER_WARN_CM} watch, "
               f"below {WATER_CRIT_CM} critical -> flood watch.")
    else:
        f = feats(p=r(995, 1025), trend=r(-3.0, 0.5, 2), t=t,
                  w=r(10, WATER_WARN_CM - 1), rise=r(WATER_RISE_WARN, 2.0, 2))
        why = (f"rise {f['water_rise_cm_per_min']} cm/min: above {WATER_RISE_WARN} "
               f"watch rate; water {f['water_level_cm']} cm below {WATER_WARN_CM} "
               "-> flood watch.")
    assert rule_engine(f) == "issue_flood_watch"
    rows.append(row(f, "issue_flood_watch", why,
        "Water level elevated or rising quickly toward overflow threshold.",
        {"water_level_cm": f["water_level_cm"],
         "water_rise_cm_per_min": f["water_rise_cm_per_min"]}))

# 5) Flood warning — 150 (pressure spans wide — water is the only signal)
for _ in range(150):
    f = feats(p=r(995, 1025), trend=r(-5.0, 0.5, 2), t=r(15, 32),
              w=r(WATER_CRIT_CM, WATER_CRIT_CM + 20), rise=r(0.2, 3.0, 2))
    assert rule_engine(f) == "issue_flood_warning"
    rows.append(row(f, "issue_flood_warning",
        f"water {f['water_level_cm']} cm: at or above {WATER_CRIT_CM} critical "
        "-> flood warning.",
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

# 7) Sensor-status faults — 70 (status note now leads the prompt — see
#    fg_core.build_prompt; pressure spans wide so status is the only signal)
for _ in range(70):
    status = random.choice(["ADC_ERROR"] * 2 + ["STALE"])
    f = feats(p=r(990, 1025), trend=r(-2.5, 2.5, 2), t=r(15, 32),
              w=r(8, 30), rise=r(-0.2, 0.3, 2), status=status)
    assert rule_engine(f) == "escalate_to_cloud"
    rows.append(row(f, "escalate_to_cloud",
        f"sensor status {status}: water data untrustworthy -> escalate.",
        "Escalate for verification.", {"reason": f"Sensor status {status}; "
                                                  "water data not trustworthy."}))

# 8) Threshold-contrast pairs — 36 (the most targeted fix for the water
#    classes: pairs of near-identical situations that differ only by crossing
#    a threshold, so the model must map the exact numbers: 24.9 -> all clear,
#    25.1 -> flood watch; 34.9 -> watch, 35.1 -> warning; rise 0.49 vs 0.51;
#    trend -1.9 vs -2.1. Plus precedence cases where water AND storm signals
#    conflict (water wins — it is checked first in rule_engine).)
def contrast_pair(lo_f, lo_dec, hi_f, hi_dec, lo_why, hi_why):
    rows.append(row(lo_f, lo_dec, lo_why, f"Threshold boundary: {lo_dec.replace('_', ' ')}."))
    rows.append(row(hi_f, hi_dec, hi_why, f"Threshold boundary: {hi_dec.replace('_', ' ')}."))

for _ in range(10):      # water watch boundary: 24.x vs 25.x
    w_lo, w_hi = r(WATER_WARN_CM - 1.0, WATER_WARN_CM - 0.05, 1), r(WATER_WARN_CM + 0.05, WATER_WARN_CM + 1.0, 1)
    lo = feats(p=r(1000, 1020), trend=r(-1.2, 0.5, 2), t=r(15, 32), w=w_lo, rise=r(0.0, 0.3, 2))
    hi = feats(p=lo["pressure_hpa"], trend=lo["pressure_trend_hpa_per_hr"], t=lo["temperature_c"], w=w_hi, rise=lo["water_rise_cm_per_min"])
    contrast_pair(lo, "report_all_clear", hi, "issue_flood_watch",
        f"water {lo['water_level_cm']} cm below {WATER_WARN_CM} watch -> all clear.",
        f"water {hi['water_level_cm']} cm at or above {WATER_WARN_CM} watch -> flood watch.")

for _ in range(10):      # rise-rate boundary: 0.49 vs 0.51
    rise_lo, rise_hi = r(WATER_RISE_WARN - 0.1, WATER_RISE_WARN - 0.01, 2), r(WATER_RISE_WARN + 0.01, WATER_RISE_WARN + 0.1, 2)
    lo = feats(p=r(1000, 1020), trend=r(-1.2, 0.5, 2), t=r(15, 32), w=r(12, 22), rise=rise_lo)
    hi = feats(p=lo["pressure_hpa"], trend=lo["pressure_trend_hpa_per_hr"], t=lo["temperature_c"], w=lo["water_level_cm"], rise=rise_hi)
    contrast_pair(lo, "report_all_clear", hi, "issue_flood_watch",
        f"rise {lo['water_rise_cm_per_min']} cm/min below {WATER_RISE_WARN} watch rate -> all clear.",
        f"rise {hi['water_rise_cm_per_min']} cm/min at or above {WATER_RISE_WARN} watch rate -> flood watch.")

for _ in range(8):       # warning boundary: 34.x vs 35.x
    w_lo, w_hi = r(WATER_CRIT_CM - 1.0, WATER_CRIT_CM - 0.05, 1), r(WATER_CRIT_CM + 0.05, WATER_CRIT_CM + 1.0, 1)
    lo = feats(p=r(1000, 1020), trend=r(-1.0, 0.5, 2), t=r(15, 32), w=w_lo, rise=r(0.2, 0.8, 2))
    hi = feats(p=lo["pressure_hpa"], trend=lo["pressure_trend_hpa_per_hr"], t=lo["temperature_c"], w=w_hi, rise=lo["water_rise_cm_per_min"])
    contrast_pair(lo, "issue_flood_watch", hi, "issue_flood_warning",
        f"water {lo['water_level_cm']} cm below {WATER_CRIT_CM} critical -> flood watch.",
        f"water {hi['water_level_cm']} cm at or above {WATER_CRIT_CM} critical -> flood warning.")

for _ in range(8):       # storm boundary: -1.9 vs -2.1
    tr_lo, tr_hi = r(-PRESSURE_DROP_STORM + 0.1, -PRESSURE_DROP_STORM + 0.01, 2), r(-PRESSURE_DROP_STORM - 0.01, -PRESSURE_DROP_STORM - 0.1, 2)
    lo = feats(p=r(1000, 1020), trend=tr_lo, t=r(15, 32), w=r(8, 20), rise=r(0.0, 0.3, 2))
    hi = feats(p=lo["pressure_hpa"], trend=tr_hi, t=lo["temperature_c"], w=lo["water_level_cm"], rise=lo["water_rise_cm_per_min"])
    contrast_pair(lo, "report_all_clear", hi, "issue_storm_watch",
        f"trend {lo['pressure_trend_hpa_per_hr']} within +/-{PRESSURE_DROP_STORM} -> all clear.",
        f"trend {hi['pressure_trend_hpa_per_hr']} at or below -{PRESSURE_DROP_STORM} -> storm watch.")

for _ in range(6):      # precedence: water signal beats storm signal
    f = feats(p=r(995, 1025), trend=r(-6.0, -2.5, 2), t=r(15, 32),
              w=r(WATER_WARN_CM, WATER_CRIT_CM - 1), rise=r(0.2, 0.8, 2))
    assert rule_engine(f) == "issue_flood_watch"
    rows.append(row(f, "issue_flood_watch",
        f"water {f['water_level_cm']} cm at or above {WATER_WARN_CM} AND trend "
        f"{f['pressure_trend_hpa_per_hr']} below -{PRESSURE_DROP_STORM} -> flood "
        "watch wins (water checked first).",
        "Water level elevated (watch); flood watch takes precedence over storm watch.",
        {"water_level_cm": f["water_level_cm"],
         "water_rise_cm_per_min": f["water_rise_cm_per_min"]}))
    f2 = feats(p=r(995, 1025), trend=r(-6.0, -2.5, 2), t=r(15, 32),
               w=r(WATER_CRIT_CM, WATER_CRIT_CM + 10), rise=r(0.5, 2.0, 2))
    assert rule_engine(f2) == "issue_flood_warning"
    rows.append(row(f2, "issue_flood_warning",
        f"water {f2['water_level_cm']} cm at or above {WATER_CRIT_CM} critical "
        "AND trend falling -> flood warning wins (highest severity first).",
        "Water level critical; flood warning takes precedence over storm watch.",
        {"water_level_cm": f2["water_level_cm"]}))

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
