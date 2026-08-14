#!/usr/bin/env python3
"""
check_classes.py — fast per-class probe of a tuned model on the classes the
45M model struggles with (flood_watch, flood_warning, escalate_to_cloud) plus
the strong classes, using FRESH scenarios (same generator as eval_model).

Usage:
  .venv/bin/python check_classes.py [--weights floodgate_v4.cact] [--n 20]
"""
import argparse
import random
import time
from collections import Counter

import edge_brain
import fg_core
from eval_model import make_scenario, CLASSES

WEAK = ["issue_flood_watch", "issue_flood_warning", "escalate_to_cloud"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=None)
    ap.add_argument("--n", type=int, default=20, help="scenarios per class")
    args = ap.parse_args()

    print(f"loading [{args.weights or 'base'}] ...")
    client = edge_brain.InferenceClient(weights=args.weights)
    random.seed(7)  # same seed as eval_model

    rows = []
    for kind in CLASSES:
        for _ in range(args.n):
            f = make_scenario(kind)
            truth = fg_core.rule_engine(f)
            try:
                resp = client.complete(fg_core.build_prompt(f))
                calls = resp.get("function_calls") or []
                pred = calls[0]["name"] if calls else "no_call"
            except Exception:
                pred = "no_call"
            rows.append((kind, truth, pred))
    client.kill()

    per = Counter()
    ok = Counter()
    for kind, truth, pred in rows:
        per[truth] += 1
        if pred == truth:
            ok[truth] += 1
    print(f"\nper-class exact-match (n={args.n} per class, fresh scenarios):")
    for c in CLASSES:
        tag = "  <-- WEAK" if c in WEAK else ""
        print(f"  {c:22s} {ok[c]:3d}/{per[c]:3d}  ({100*ok[c]/max(per[c],1):5.1f}%){tag}")
    t = sum(ok.values()); n = sum(per.values())
    print(f"  {'OVERALL':22s} {t:3d}/{n:3d}  ({100*t/max(n,1):5.1f}%)")


if __name__ == "__main__":
    main()
