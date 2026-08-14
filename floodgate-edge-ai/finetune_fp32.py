#!/usr/bin/env python3
"""
finetune_fp32.py — patched LoRA trainer for Needle 2 that fixes the loss=nan
bug in `needle finetune` on half-precision checkpoints.

ROOT CAUSE (verified by reproduction): the packaged trainer casts the LoRA
A/B matrices to the base checkpoint's dtype (float16). AdamW's eps=1e-8 is
subnormal in fp16, so the second-moment math under/overflows on the very
first optimizer update -> loss is finite at step 1 and nan from step 2 on.

FIX: keep the LoRA adapter (and optimizer state) in float32; merge_lora
already casts the A@B delta back to the checkpoint dtype for the forward
pass. Also: gradient clipping, loss computed in f32, and a hard nan guard
that aborts instead of silently saving a poisoned adapter.

Saves the adapter in the exact pkl format `needle build --lora` expects.

Usage (same shape as the CLI):
  python3 finetune_fp32.py finetune_data.jsonl --out floodgate_lora.pkl
  needle build checkpoints/needle2.pkl --lora floodgate_lora.pkl --out floodgate.cact --bits 2
"""
import argparse
import os
import pickle

import numpy as np

# Reuse the package's own internals so formats stay in lockstep.
from needle.model.run import load_checkpoint
from needle.model.finetune import (get_tokenizer, load_jsonl,
                                   lora_target_paths, merge_lora)
from needle.model.architecture import SimpleAttentionNetwork


def init_lora_fp32(params, paths, rank, key):
    """Same as needle's init_lora but WITHOUT the .astype(weight.dtype) cast."""
    import jax
    import jax.numpy as jnp
    from flax.traverse_util import flatten_dict
    flat = flatten_dict(params)
    lora = {}
    for path in paths:
        weight = flat[path]
        in_dim, out_dim = weight.shape[-2], weight.shape[-1]
        lead = weight.shape[:-2]
        key, sub = jax.random.split(key)
        lora[path] = {
            "A": jax.random.normal(sub, lead + (in_dim, rank), jnp.float32) / rank,
            "B": jnp.zeros(lead + (rank, out_dim), jnp.float32),
        }
    return lora


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl_path")
    ap.add_argument("--checkpoint", default="checkpoints/needle2.pkl",
                    help="base checkpoint (auto-downloads from HF if missing)")
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--lora-rank", type=int, default=16)
    ap.add_argument("--lora-alpha", type=float, default=32.0)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--clip", type=float, default=1.0, help="global grad-norm clip")
    ap.add_argument("--out", default="floodgate_lora.pkl")
    ap.add_argument("--class-weights",
                    help="per-class loss weights, e.g. "
                         "'report_all_clear=0.6,issue_flood_warning=1.4'. "
                         "Scales each example's loss by its answer class weight, "
                         "so the model works harder on under-represented/weak classes.")
    args = ap.parse_args()

    import json
    import jax
    import jax.numpy as jnp
    import optax

    params, config = load_checkpoint(args.checkpoint)
    params = jax.device_put(params)
    ckpt_dtype = jax.tree_util.tree_leaves(params)[0].dtype
    print(f"checkpoint dtype: {ckpt_dtype} (LoRA + optimizer will stay float32)")

    tokenizer = get_tokenizer(config.vocab_size)
    seqs, masks = load_jsonl(args.jsonl_path, tokenizer, args.max_len)
    if len(seqs) == 0:
        raise SystemExit("no usable examples in " + args.jsonl_path)
    print(f"training on {len(seqs)} examples, seq_len {args.max_len}")

    # Optional per-class loss weighting — aligned to rows in file order,
    # same filter load_jsonl applies (rows without "query" are skipped).
    ex_weights = None
    if args.class_weights:
        wmap = {}
        for tok in args.class_weights.split(","):
            k, v = tok.split("=")
            wmap[k.strip()] = float(v)
        rows_used = 0
        for line in open(args.jsonl_path):
            line = line.strip()
            if not line:
                continue
            ex = json.loads(line)
            if "query" not in ex:
                continue
            rows_used += 1
        wlist = []
        with open(args.jsonl_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                ex = json.loads(line)
                if "query" not in ex:
                    continue
                name = (ex.get("answers") or [{}])[0].get("name", "")
                wlist.append(wmap.get(name, 1.0))
        ex_weights = jnp.asarray(wlist, jnp.float32)
        print(f"class weights applied ({len(wlist)} rows, "
              f"rows_used check={rows_used == len(wlist)})")

    model = SimpleAttentionNetwork(config)
    paths = lora_target_paths(params)
    scale = args.lora_alpha / args.lora_rank
    lora = init_lora_fp32(params, paths, args.lora_rank, jax.random.PRNGKey(0))
    print(f"LoRA rank {args.lora_rank} on {len(paths)} weight groups, "
          f"clip {args.clip}, lr {args.lr} (compiling...)")

    optimizer = optax.chain(optax.clip_by_global_norm(args.clip),
                            optax.adamw(args.lr))
    opt_state = optimizer.init(lora)

    def loss_fn(lora, ids, mask, exw=None):
        logits = model.apply({"params": merge_lora(params, lora, scale)}, ids)
        logits = logits.astype(jnp.float32)          # CE in f32 for stability
        logits, targets, mask = logits[:, :-1], ids[:, 1:], mask[:, 1:]
        ce = optax.softmax_cross_entropy_with_integer_labels(logits, targets)
        if exw is not None:                          # per-example class weighting
            mask = mask * exw[:, None]
        return (ce * mask).sum() / jnp.maximum(mask.sum(), 1.0)

    @jax.jit
    def train_step(lora, opt_state, ids, mask, exw):
        loss, grads = jax.value_and_grad(loss_fn)(lora, ids, mask, exw)
        updates, opt_state = optimizer.update(grads, opt_state, lora)
        return optax.apply_updates(lora, updates), opt_state, loss

    batch, count = args.batch_size, len(seqs)
    steps_per_epoch = -(-count // batch)
    total_steps = args.epochs * steps_per_epoch
    every = max(1, total_steps // 50)
    step_i, last = 0, 0.0
    for epoch in range(args.epochs):
        order = np.random.permutation(count)
        for start in range(0, count, batch):
            idx = order[start:start + batch]
            ew = ex_weights[idx] if ex_weights is not None else None
            lora, opt_state, loss = train_step(lora, opt_state,
                                               jnp.asarray(seqs[idx]),
                                               jnp.asarray(masks[idx]), ew)
            last = float(loss)
            step_i += 1
            if not np.isfinite(last):
                raise SystemExit(
                    f"loss became {last} at step {step_i} — aborting WITHOUT "
                    "saving. Retry with --lr 3e-5 (and/or --clip 0.5).")
            if step_i % every == 0:
                print(f"epoch {epoch + 1}/{args.epochs}  step {step_i}/{total_steps}  loss {last:.4f}",
                      flush=True)
        print(f"epoch {epoch + 1}/{args.epochs}  loss {last:.4f}", flush=True)

    # Final sanity: refuse to save non-finite adapters.
    for p, v in lora.items():
        for k in ("A", "B"):
            if not np.isfinite(np.asarray(v[k])).all():
                raise SystemExit(f"non-finite values in adapter {p}/{k}; not saving.")

    with open(args.out, "wb") as handle:
        pickle.dump({
            "lora": {"/".join(p): {"A": np.asarray(v["A"], np.float32),
                                   "B": np.asarray(v["B"], np.float32)}
                     for p, v in lora.items()},
            "scale": float(scale),
            "base": args.checkpoint,
            "rank": args.lora_rank,
        }, handle)
    print(f"saved LoRA adapter -> {args.out}  (final loss {last:.4f})")
    print(f"merge + export with: needle build {args.checkpoint} --lora {args.out} --out floodgate.cact --bits 2")


if __name__ == "__main__":
    main()
