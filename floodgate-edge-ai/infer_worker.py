#!/usr/bin/env python3
"""
infer_worker.py — crash-isolated Needle 2 inference worker.

WHY THIS EXISTS (war story, see TUTORIAL.md §9):
The needle engine dylibs on macOS segfault after ~6-8 inferences when
external .cact weights are loaded (verified on engine 2.0.0 and 2.0.3;
the baked-in base-model path is unaffected). A segfault kills the whole
process, so running inference in-process would take down the edge brain
and the eval harness after a few decisions. Instead, inference runs here,
in a small dedicated subprocess that the parent supervises: if this worker
dies, the parent restarts it and retries the prompt. This is also good
practice for a long-running edge service (defense in depth on any platform).

Protocol (JSON lines over stdin/stdout):
  in:  {"prompt": "..."}                      one request per line
  out: {"ok": true, "raw": {...}}             the needle response dict
       {"ok": false, "error": "..."}          a handled failure

The worker holds ONE needle.Needle instance; each request is answered
statelessly (reset() before answering, like the original eval loop).

Usage:
  python3 infer_worker.py --weights floodgate.cact   # weights optional
"""
import argparse
import json
import sys

import needle

import edge_brain  # TOOLS, SYSTEM (imports fg_core)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--weights", default=None)
    args = ap.parse_args()

    nd = needle.Needle(tools=edge_brain.TOOLS, system=edge_brain.SYSTEM,
                       weights=args.weights)
    sys.stderr.write(f"infer_worker ready (weights={args.weights or 'base'})\n")
    sys.stderr.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = nd.complete(req["prompt"])
            nd.reset()
            sys.stdout.write(json.dumps({"ok": True, "raw": resp}) + "\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stdout.write(json.dumps({"ok": False, "error": str(e)}) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
