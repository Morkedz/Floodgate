#!/usr/bin/env python3
"""
diagnose_cact.py — pinpoint why needle_load rejects an exported .cact.

Runs four isolated checks (each weight-load happens in a SUBPROCESS so a
rejected/corrupt blob can't poison the next test):

  1. Base engine sanity: Needle() with baked-in weights.
  2. Download Cactus's OFFICIAL prebuilt .cact from the HF repo and load it.
       -> loads:  the engine accepts external blobs; problem is OUR export.
       -> fails:  engine 2.0.1 can't load ANY external blob produced/paired
                  with package 2.0.2 (version skew); pin cactus-needle==2.0.1.
  3. Parse + diff the binary headers/directories of official vs ours
     (tag, tensor count, codebook, kv fields, per-dtype tensor census).
  4. Load-test each .cact you pass on the command line.

Usage:
  python3 diagnose_cact.py floodgate.cact [more.cact ...]
"""
import glob
import json
import os
import struct
import subprocess
import sys

HDR_FMT = "<IIIII"          # tag, num_tensors, codebook_len, kv_window, kv_bits
REC_FMT = "<BBHIIIIQQII"    # dtype, ndim, _pad, shape[4], offset, nbytes, group, bits
REC_SIZE = struct.calcsize(REC_FMT)
TAG = 0x05E12A82
DTYPES = {1: "FP16", 2: "FP32", 3: "CQ", 4: "RAW"}


def parse_header(path):
    with open(path, "rb") as f:
        blob = f.read()
    tag, n, cb_len, kv_win, kv_bits = struct.unpack_from(HDR_FMT, blob, 0)
    off = struct.calcsize(HDR_FMT) + cb_len * 4
    recs = []
    for i in range(n):
        vals = struct.unpack_from(REC_FMT, blob, off + i * REC_SIZE)
        dtype, ndim = vals[0], vals[1]
        shape = vals[3:7][:max(ndim, 1)]
        recs.append({"dtype": DTYPES.get(dtype, f"?{dtype}"), "ndim": ndim,
                     "shape": shape, "nbytes": vals[8], "group": vals[9],
                     "bits": vals[10]})
    census = {}
    for r in recs:
        key = f"{r['dtype']}" + (f"/b{r['bits']}" if r["dtype"] == "CQ" else "")
        census[key] = census.get(key, 0) + 1
    return {"file": path, "size_mb": round(len(blob) / 1e6, 2),
            "tag_ok": tag == TAG, "tag": f"0x{tag:08X}", "num_tensors": n,
            "codebook_len": cb_len, "kv_window": kv_win, "kv_bits": kv_bits,
            "census": census, "first3": recs[:3], "last3": recs[-3:]}


def try_load(path):
    """Attempt needle_load in a clean subprocess. Returns (ok, output)."""
    code = (
        "import needle\n"
        f"needle.Needle(weights={path!r}, tools=[], system='')\n"
        "print('LOAD_OK')\n"
    )
    p = subprocess.run([sys.executable, "-c", code],
                       capture_output=True, text=True, timeout=600)
    ok = "LOAD_OK" in p.stdout
    return ok, (p.stdout + p.stderr).strip()[-400:]


def main():
    ours = sys.argv[1:] or sorted(glob.glob("*.cact"))

    print("== 1. Base engine (baked-in weights, no needle_load) ==")
    code = "import needle; needle.Needle(tools=[], system=''); print('BASE_OK')"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    base_ok = "BASE_OK" in p.stdout
    print("   PASS" if base_ok else f"   FAIL\n{(p.stdout + p.stderr)[-400:]}")

    print("\n== 2. Official prebuilt .cact from HF ==")
    official = None
    try:
        from huggingface_hub import list_repo_files, hf_hub_download
        repo = "Cactus-Compute/needle2"
        cacts = [f for f in list_repo_files(repo) if f.endswith(".cact")]
        print(f"   .cact files in {repo}: {cacts or 'NONE'}")
        if cacts:
            official = hf_hub_download(repo, cacts[0])
            ok, out = try_load(official)
            print(f"   load {os.path.basename(official)}: {'PASS' if ok else 'FAIL'}")
            if not ok:
                print("   " + out.replace("\n", "\n   "))
                print("\n   >>> The engine can't load even the OFFICIAL blob.")
                print("   >>> Version skew: pin the whole toolchain to the engine:")
                print("   >>>   pip install 'cactus-needle==2.0.1' && retrain/rebuild")
    except Exception as e:
        print(f"   (skipped: {e})")

    print("\n== 3. Header comparison ==")
    for path in ([official] if official else []) + ours:
        if path and os.path.exists(path):
            h = parse_header(path)
            print(json.dumps(h, indent=2, default=str))

    print("\n== 4. Load-test your exports ==")
    for path in ours:
        if not os.path.exists(path):
            continue
        ok, out = try_load(path)
        print(f"   {path}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            print("   " + out.replace("\n", "\n   "))

    print("\n== Interpretation cheat-sheet ==")
    print(" official PASS + ours FAIL, num_tensors differ  -> exporter canon-order drift;")
    print("                                                   pin cactus-needle==2.0.1, rebuild")
    print(" official PASS + ours FAIL, census differs      -> quantization scheme mismatch;")
    print("                                                   rebuild WITHOUT --bits 2 (mixed map):")
    print("                                                   needle build checkpoints/needle2.pkl \\")
    print("                                                     --lora floodgate_lora.pkl --out floodgate.cact")
    print(" official FAIL                                  -> engine/exporter version skew;")
    print("                                                   pip install 'cactus-needle==2.0.1',")
    print("                                                   then retrain + rebuild in that env")
    print(" base FAIL                                      -> engine/platform problem; report to Cactus")


if __name__ == "__main__":
    main()
