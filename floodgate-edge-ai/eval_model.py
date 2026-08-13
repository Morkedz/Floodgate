#!/usr/bin/env python3
"""
eval_model.py — measure whether the model is actually good.

Generates N fresh held-out scenarios (same physics/thresholds as training but
new random values + fuzzy near-threshold cases the model never saw), asks the
model to decide, and scores it against the deterministic rule engine (ground
truth, fg_core.rule_engine). Prompts are built with fg_core.build_prompt —
the exact runtime query format. Reports per-class accuracy, a confusion
matrix, latency, and a separate "adversarial" check (frozen / conflicting /
spiking sensors that fool the rules — the model should escalate them).

Classes now include escalate_to_cloud for SENSOR-FAULT scenarios (status
ADC_ERROR / STALE), which the rule engine also returns — the aligned system's
"never alert on garbage data" behavior.

Run both ways and compare — this is your competition slide:
  python3 eval_model.py --weights floodgate.cact      # fine-tuned
  python3 eval_model.py                               # base model
"""
import argparse
import random
import time
from collections import Counter, defaultdict

import edge_brain  # reuses TOOLS, SYSTEM, InferenceClient
import fg_core     # rule_engine, build_prompt, thresholds

CLASSES = ["report_all_clear", "issue_storm_watch",
           "issue_flood_watch", "issue_flood_warning", "escalate_to_cloud"]


def r(a, b, nd=1):
    return round(random.uniform(a, b), nd)


def base_feats(status="OK", weather=0):
    return {"sensor_status": status, "weather_high_risk": weather,
            "device_id": "ESP32-FloodGate", "samples_in_window": 99}


def make_scenario(kind):
    """Fresh scenarios incl. near-threshold cases NOT in the training set."""
    B = fg_core
    if kind == "report_all_clear":
        f = dict(pressure_hpa=r(1004, 1026), pressure_trend_hpa_per_hr=r(-1.5, 1.5, 2),
                 temperature_c=r(10, 35), water_level_cm=r(4, B.WATER_WARN_CM - 2),
                 water_rise_cm_per_min=r(-0.15, 0.3, 2))
    elif kind == "issue_storm_watch":
        f = dict(pressure_hpa=r(988, 1013), pressure_trend_hpa_per_hr=r(-9.0, -B.PRESSURE_DROP_STORM - 0.1, 2),
                 temperature_c=r(10, 35), water_level_cm=r(4, B.WATER_WARN_CM - 2),
                 water_rise_cm_per_min=r(-0.15, 0.3, 2))
    elif kind == "issue_flood_watch":
        if random.random() < 0.5:
            f = dict(water_level_cm=r(B.WATER_WARN_CM + 0.5, B.WATER_CRIT_CM - 1),
                     water_rise_cm_per_min=r(0.0, 0.4, 2))
        else:
            f = dict(water_level_cm=r(8, B.WATER_WARN_CM - 2),
                     water_rise_cm_per_min=r(B.WATER_RISE_WARN + 0.05, 2.5, 2))
        f.update(pressure_hpa=r(995, 1020), pressure_trend_hpa_per_hr=r(-1.9, 0.5, 2),
                 temperature_c=r(10, 35))
    elif kind == "issue_flood_warning":
        f = dict(pressure_hpa=r(990, 1016), pressure_trend_hpa_per_hr=r(-4.0, 0.5, 2),
                 temperature_c=r(10, 35), water_level_cm=r(B.WATER_CRIT_CM + 0.5, B.WATER_CRIT_CM + 25),
                 water_rise_cm_per_min=r(0.2, 3.5, 2))
    else:  # escalate_to_cloud: sensor faults (status != OK)
        f = dict(pressure_hpa=r(1000, 1018), pressure_trend_hpa_per_hr=r(-2.5, 2.5, 2),
                 temperature_c=r(10, 35), water_level_cm=r(6, 32),
                 water_rise_cm_per_min=r(-0.2, 0.5, 2))
        f.update(base_feats(status=random.choice(["ADC_ERROR", "STALE"])))
        return f
    f.update(base_feats())
    return f


def prompt_of(f):
    return fg_core.build_prompt(f)


def make_adversarial():
    """Cases that fool the rule engine; the model should escalate them."""
    B = fg_core
    cases = [
        dict(pressure_hpa=1010.0, pressure_trend_hpa_per_hr=0.0, temperature_c=22.0,
             water_level_cm=15.0, water_rise_cm_per_min=0.0),          # frozen
        dict(pressure_hpa=1000.0, pressure_trend_hpa_per_hr=-4.5, temperature_c=21.0,
             water_level_cm=8.0, water_rise_cm_per_min=-1.0),          # conflict
        dict(pressure_hpa=1010.0, pressure_trend_hpa_per_hr=0.1, temperature_c=22.0,
             water_level_cm=75.0, water_rise_cm_per_min=10.0),         # spike
    ]
    out = []
    for f in cases:
        f.update(base_feats())
        out.append((f, "escalate_to_cloud"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=None, help=".cact path (omit for base model)")
    ap.add_argument("--n", type=int, default=40, help="scenarios per class")
    args = ap.parse_args()

    print(f"Loading model [{args.weights or 'base'}]...")
    # Inference runs in a crash-restartable worker (see edge_brain.InferenceClient):
    # the needle engine segfaults after ~6-8 in-process inferences with external
    # .cact weights on macOS, which would kill the eval mid-run.
    client = edge_brain.InferenceClient(weights=args.weights)

    confusion = defaultdict(Counter)
    latencies = []
    random.seed(7)  # same eval set for base vs tuned comparison

    total = args.n * len(CLASSES)
    done = 0
    parse_fail = 0
    for kind in CLASSES:
        for _ in range(args.n):
            f = make_scenario(kind)
            truth = fg_core.rule_engine(f)
            t0 = time.perf_counter()
            try:
                resp = client.complete(prompt_of(f))
            except Exception:
                # worker could not produce a response (engine crash etc.)
                resp = {}
                parse_fail += 1
            latencies.append((time.perf_counter() - t0) * 1000)
            calls = resp.get("function_calls") or []
            pred = calls[0]["name"] if calls else "no_call"
            confusion[truth][pred] += 1
            done += 1
            if done % 20 == 0:
                print(f"  {done}/{total} ...")
    if parse_fail:
        print(f"  (note: {parse_fail} responses were unavailable and counted as no_call)")

    # Adversarial checks: truth is escalate_to_cloud by construction.
    adv = make_adversarial()
    adv_ok = 0
    for f, _ in adv:
        try:
            resp = client.complete(prompt_of(f))
        except Exception:
            resp = {}
        calls = resp.get("function_calls") or []
        pred = calls[0]["name"] if calls else "no_call"
        if pred == "escalate_to_cloud":
            adv_ok += 1
    client.kill()

    print("\n=== Per-class accuracy (vs rule engine ground truth) ===")
    overall_ok = overall_n = 0
    for truth in CLASSES:
        row = confusion[truth]
        n = sum(row.values())
        ok = row[truth]
        overall_ok += ok; overall_n += n
        print(f"  {truth:22s} {ok:3d}/{n:<3d} ({100*ok/max(n,1):5.1f}%)")
    print(f"  {'OVERALL':22s} {overall_ok:3d}/{overall_n:<3d} ({100*overall_ok/max(overall_n,1):5.1f}%)")

    print("\n=== Confusion matrix (rows=truth, cols=predicted) ===")
    labels = CLASSES + ["no_call"]
    short = {c: c.replace("report_", "").replace("issue_", "")[:12] for c in labels}
    print(" " * 16 + "".join(f"{short[c]:>14s}" for c in labels))
    for truth in CLASSES:
        print(f"{short[truth]:>15s} " + "".join(f"{confusion[truth][c]:>14d}" for c in labels))

    print(f"\n=== Adversarial escalation (frozen/conflict/spike, expect escalate) ===")
    print(f"  model escalated {adv_ok}/{len(adv)} adversarial cases")

    lat = sorted(latencies)
    print(f"\n=== Latency ===  median {lat[len(lat)//2]:.0f} ms   "
          f"p95 {lat[int(len(lat)*0.95)]:.0f} ms   n={len(lat)}")
    print("\nNote: 'escalate_to_cloud' on clear-cut cases counts as a miss here,")
    print("but in the live system it fails safe (rule engine still drives alerts).")


if __name__ == "__main__":
    main()
