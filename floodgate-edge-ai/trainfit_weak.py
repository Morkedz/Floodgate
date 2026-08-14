#!/usr/bin/env python3
"""
trainfit_weak.py — does the tuned model MEMORIZE the weak classes?

Samples N rows per weak class straight from the training set and measures
exact-match. High train-fit = the model can learn the mapping but doesn't
generalize (data/eval gap). Low train-fit = capacity or data problem.

Usage:
  .venv/bin/python trainfit_weak.py [--weights floodgate_v4.cact] [--n 20]
"""
import argparse
import json
import random

import edge_brain

WEAK = ["issue_flood_watch", "issue_flood_warning", "escalate_to_cloud"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=None)
    ap.add_argument("--n", type=int, default=20)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open("finetune_data.jsonl")]
    by_class = {}
    for r in rows:
        name = r["answers"][0]["name"]
        by_class.setdefault(name, []).append(r)
    random.seed(3)
    sample = []
    for c in WEAK:
        sample += random.sample(by_class[c], min(args.n, len(by_class[c])))

    print(f"loading [{args.weights or 'base'}] ...")
    client = edge_brain.InferenceClient(weights=args.weights)
    per = {c: [0, 0] for c in WEAK}
    for r in sample:
        truth = r["answers"][0]["name"]
        try:
            resp = client.complete(r["query"])
            calls = resp.get("function_calls") or []
            pred = calls[0]["name"] if calls else "no_call"
        except Exception:
            pred = "no_call"
        per[truth][1] += 1
        if pred == truth:
            per[truth][0] += 1
    client.kill()

    print(f"\nTRAIN-set fit on weak classes (n={args.n} per class):")
    for c in WEAK:
        ok, n = per[c]
        print(f"  {c:22s} {ok:3d}/{n:3d}  ({100*ok/max(n,1):5.1f}%)")
    tot_ok = sum(v[0] for v in per.values()); tot_n = sum(v[1] for v in per.values())
    print(f"  {'TOTAL':22s} {tot_ok:3d}/{tot_n:3d}  ({100*tot_ok/max(tot_n,1):5.1f}%)")


if __name__ == "__main__":
    main()
