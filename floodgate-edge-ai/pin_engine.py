#!/usr/bin/env python3
"""
pin_engine.py — make the Needle engine match the .cact format this machine
will load. Run it ONCE on any machine that loads floodgate.cact — including
the Raspberry Pi 5 — after `pip install cactus-needle`.

THIS PROJECT LOCKS cactus-needle==2.0.2 (see DESIGN.md): 2.0.3 changed the
LoRA target groups (10 -> 5) and the quantization scheme, so every artifact
here is built with 2.0.2 and must be loaded by the 2.0.0 engine dylib, which
is the only engine that reads the 0x05E12A82 format the 2.0.2 exporter
writes. This script:

  * on cactus-needle == 2.0.2: pins the 2.0.0 engine dylib into the package
    folder (the loader prefers it) so our .cact loads — REQUIRED, no-op safe.
  * on cactus-needle != 2.0.2: prints a warning instead of silently pinning,
    because the version set is wrong for this project's artifacts.

History: the 0x05E12A82-vs-0x05E12A83 skew was a genuine vendor bug (engine
released before its matching exporter). 2.0.3 finally shipped a matched pair
at 0x05E12A83, but its trainer/quantizer changed in ways that broke our
validated pipeline, so we stay on 2.0.2 by design.

Usage:
  python3 pin_engine.py          # ensure engine/exporter match (auto)
  python3 pin_engine.py --undo   # restore the default engine unconditionally
"""
import os
import subprocess
import sys
import zipfile
from importlib.metadata import version as pkg_version

PIN_VERSION = "2.0.0"


def _needle_version() -> str:
    try:
        return pkg_version("cactus-needle")
    except Exception:
        return "?"


def main():
    from huggingface_hub import hf_hub_download
    from needle.agent.fetch import HF_REPO, _platform_tag, _lib_name
    import needle

    pkg_dir = os.path.dirname(os.path.abspath(needle.__file__))
    local = os.path.join(pkg_dir, _lib_name())
    ver = _needle_version()

    if "--undo" in sys.argv:
        if os.path.exists(local):
            os.remove(local)
            print(f"removed {local}; default engine restored")
        else:
            print("no pinned engine found; nothing to do")
        return

    try:
        major, minor, patch = (int(x) for x in ver.split(".")[:3])
        locked = (major, minor, patch) == (2, 0, 2)
    except Exception:
        locked = False

    if not locked:
        print(f"WARNING: cactus-needle is {ver}, but this project's .cact is "
              "built with cactus-needle 2.0.2 (format tag 0x05E12A82).")
        print("Install the locked toolchain and re-run:")
        print("  pip install 'cactus-needle==2.0.2' 'jax==0.11.0' 'jaxlib==0.11.0'"
              " 'flax==0.12.8' 'optax==0.2.8' 'numpy==2.5.2'")
        print("(If you intentionally built floodgate.cact with 2.0.3+, skip "
              "this script and use the default 2.0.3 engine.)")
        if os.path.exists(local):
            os.remove(local)
            print(f"removed stale pin ({local}) to avoid a broken combination.")
        return

    print(f"cactus-needle {ver}: exporter writes the 0x05E12A82 format; pinning "
          "the 2.0.0 engine dylib so our .cact loads...")
    wheel = f"python/cactus_needle-{PIN_VERSION}-py3-none-{_platform_tag()}.whl"
    print(f"fetching {wheel} from {HF_REPO} ...")
    wpath = hf_hub_download(HF_REPO, wheel, repo_type="model")
    data = zipfile.ZipFile(wpath).read("needle/" + _lib_name())
    with open(local, "wb") as f:
        f.write(data)
    print(f"pinned engine {PIN_VERSION} -> {local}")

    # verify
    code = "import needle; needle.Needle(tools=[], system=''); print('ENGINE_OK')"
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    print("engine sanity:", "PASS" if "ENGINE_OK" in p.stdout else "FAIL\n" + (p.stdout + p.stderr)[-300:])


if __name__ == "__main__":
    main()

