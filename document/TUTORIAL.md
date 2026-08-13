# FloodGate: How We Taught a 14 MB AI Model to Watch for Floods

*A tutorial walking through the whole FloodGate system — the ESP32 edge node,
the Raspberry Pi gateway, the Supabase dashboard, and the on-device AI brain
— written for a student meeting LoRA fine-tuning and edge AI for the first
time. Every file, every step, every concept.*

---

## 1. The big picture

FloodGate is a device that sits near a storm drain and watches three things:
temperature, barometric (air) pressure, and water level. Falling air pressure
is a classic sign that a storm is coming; rising water in the drain is a sign
it might overflow. The question this project answers is: **can a tiny AI
model, running on an $60 Raspberry Pi 4 with no internet connection, look at
those sensor readings and decide what to do?**

The answer is yes. The system has four layers, and the whole pipeline looks
like this:

```
 ESP32 edge node                         Raspberry Pi 4/5 gateway
 ┌──────────────────────┐   MQTT   ┌───────────────────────────────────────┐
 │ BMP280 (temp,pres)   │ ───────▶ │ floodgatebridgeandweather.py → Supabase│
 │ KIT0139 (water depth)│  topic   │ dashboard (:8080)                       │
 │ publishes every 1-5  │          │ edge_brain.py  →  fg_core features      │
 │ min (deep sleep)     │          │   ├─ rule engine (safety net, always    │
 └──────────────────────┘          │   │   drives the real alert)            │
         ▲ OpenWeather "high risk" │   ├─ Needle 2 LLM (tool calling,        │
         └──────── broadcast ◀─────┘   │   14 MB, fully offline)              │
                                      │   └─ edge/rule compare + escalate    │
                                      └───────────────────────────────────────┘
```

The dataset pipeline trains the AI on a laptop:

```
  fg_core.py  (single source of truth: thresholds, prompt format, rule engine)
        │  imported by all three stages
        ▼
  make_finetune_data.py          finetune_fp32.py            needle build
 ┌─────────────────────┐      ┌──────────────────────┐     ┌─────────────────┐
 │ Generate 475 example │ ───▶ │ LoRA fine-tune the   │ ──▶ │ Merge + shrink  │
 │ "sensor situations"  │      │ 45M-parameter model  │     │ to 4-bit weights│
 │ finetune_data.jsonl  │      │ floodgate_lora.pkl   │     │ floodgate.cact  │
 └─────────────────────┘      └──────────────────────┘     └─────────────────┘
```

Several files exist because of problems we hit along the way —
`finetune_fp32.py`, `pin_engine.py`, `diagnose_cact.py`, `infer_worker.py` —
and honestly, those debugging stories teach more about real engineering than
the happy path does. They get their own sections (7, 9).

## 2. The model: what is Needle 2, and why so small?

Most AI chatbots you've used run models with billions of parameters
("parameters" are the learned numbers inside the network — think of them as
the knobs the training process tunes). Needle 2, made by Cactus Compute, has
only 45 million parameters and deploys as a ~23 MB file. That's smaller than
a single photo burst from a phone.

It can be that small because of a clever bet: it doesn't try to know things
or chat. It does exactly one job, called **tool calling** (also called
function calling). You hand the model a menu of functions it's allowed to
call — each with a name, a description, and typed parameters — plus a
sentence describing a situation. The model's entire output is a decision:
*which function to call, with which argument values*. Turning "pressure is
dropping fast" into `issue_storm_watch(summary=..., pressure_trend_hpa_per_hr=-4.5)`
doesn't require knowing who won the World Cup. Strip away world knowledge and
open-ended chat, and 45 million parameters is enough.

That's the core insight of edge AI: **match the model size to the actual
problem.** Our problem is choosing one of five actions from a one-sentence
sensor summary. A frontier model would be like renting a stadium to play
ping-pong.

## 3. How the system talks: the data contract

Every component speaks one payload schema. The ESP32 firmware publishes:

```json
{
  "device_id": "ESP32-FloodGate",
  "water_depth": 0.245,          // METERS — the dashboard charts "Depth (m)"
  "atm_pressure_hpa": 1013.2,    // hPa
  "ambient_temp_c": 24.1,        // deg C
  "status": "OK"                 // "OK" | "ADC_ERROR"
}
```

over MQTT on topic `umd/cpse/floodgate/telemetry`. The OpenWeather bridge
publishes `{"source": "openweather_api", "high risk": 0|1}` on the *same*
topic, and the firmware even shortens its deep-sleep cadence when that flag
is high (1 minute instead of 5). One bus, three listeners: the bridge, the
dashboard, and the AI brain.

Three conventions make this contract safe:

1. **Units convert at the boundary, once.** The firmware measures depth in
   meters; the brain's thresholds and training vocabulary are in
   centimeters (watch 25 cm / critical 35 cm). `fg_core.parse_payload()`
   does `cm = m × 100` in exactly one place. Everything downstream — rules,
   prompts, training data, evaluation — thinks in centimeters only. The
   general pattern: pick one internal unit and translate at the door.
2. **The sensor tells you when it's lying.** `status: "ADC_ERROR"` means the
   water transducer is unreadable; `STALE` (no reading for 15 minutes —
   three missed 5-minute sleep cycles) means the device went quiet. Both are
   first-class features, and the rule engine escalates on bad status *before*
   any water rule can fire: we never raise a flood alert on data we know is
   garbage.
3. **One source of truth.** All thresholds, the payload parser, the feature
   extractor, the rule engine, and the exact LLM prompt format live in
   `fg_core.py`. The dataset generator, the runtime and the eval harness all
   import it, so training and deployment *cannot* drift apart. The dataset
   generator doesn't even copy the query format — it calls the same
   `build_prompt()` the runtime uses.

The general lesson for any student project that grows: **when components
share data, the schema is a contract, and contracts drift unless one file is
the single source of truth.**

## 4. The brain: `edge_brain.py`

This is the program that runs forever on the Raspberry Pi gateway. It has
five jobs, and reading it top to bottom you'll find them in this order.

**Collecting readings.** In the recommended mode (`--mqtt`) the brain
subscribes to the same MQTT topic the firmware publishes to — so wiring the
AI into the live system requires **zero firmware changes**. The OpenWeather
broadcasts arrive on the same subscription and are routed to a weather flag
instead of being mistaken for readings. An HTTP alternative
(`esp32_sender.ino` POSTing the same schema to `:8090/reading`) exists for
broker-less setups, and `--simulate` fakes four phases of weather and
hardware (normal, storm approaching, flash flood, sensor fault) so you can
develop and demo without any hardware at all. Professional teams always
build a simulator; hardware is slow to set up and hard to make misbehave on
demand.

**Computing features.** Raw readings aren't what matters — *trends* are. The
`SensorWindow.features()` method computes the rate of change: pressure in
hPa per hour, water rise in cm per minute. This is called feature
engineering, and it's often more important than the model itself. A pressure
of 1005 hPa means little; a pressure that *fell 5 hPa in the last hour* means
a storm. Two extra features carry the health of the system: `sensor_status`
(OK / ADC_ERROR / STALE) and `weather_high_risk` (from the bridge's
broadcast). Trends need real history — a rate computed over a few seconds of
data is noise, so anything under 20% of its nominal window reads 0.

**The rule engine — the safety net.** `rule_engine()` is about ten lines of
plain `if` statements in `fg_core.py`: water above 35 cm → flood warning;
falling faster than 2 hPa/hr → storm watch; sensor status not OK → escalate;
and so on. This deterministic logic *always* drives the real alert. Why keep
boring rules when we have an AI? Because the rules are auditable (a judge
can point at the exact line that fired), instant, and can never hallucinate.
The design principle is: **never let the AI be a single point of failure for
a safety decision.** The AI runs in parallel, its answer is compared to the
rules every cycle, and the AGREE/DISAGREE rate is printed live.

**The tools.** Five Python functions decorated with `@needle.tool`:
`report_all_clear`, `issue_storm_watch`, `issue_flood_watch`,
`issue_flood_warning`, and `escalate_to_cloud`. The decorator reads each
function's type hints and docstring and turns them into a schema — a
machine-readable menu card — that the model receives with every prompt. This
is worth pausing on: **the docstrings are not comments, they're the model's
instructions.** "Issue a storm watch because barometric pressure is falling
fast, which precedes storms" is text the model actually reads when deciding.

**Edge-cloud collaboration.** When the model is unsure — it calls
`escalate_to_cloud`, disagrees with the rule engine, reports low confidence,
or the sensor status is faulty — the system can optionally send the
situation to a big cloud model (Anthropic API) for a deeper second opinion.
The philosophy: routine decisions stay local, private, instant, and free;
rare hard cases get frontier-model help. The system still works with the
WiFi cable pulled out — for flood infrastructure, where storms are exactly
when connectivity dies, offline-first isn't a nice-to-have, it's the safety
requirement.

**Crash-isolated inference.** The Needle engine on macOS was found to
segfault after a handful of inferences when external model weights are
loaded. Inference therefore runs in a supervised child process
(`infer_worker.py`, managed by `InferenceClient`): if the worker dies, the
parent restarts it and retries; the rule engine keeps driving alerts
regardless. A third-party native library never runs in the main process
unprotected again (full story in section 9).

## 5. Why an AI at all? What the if/else rules can't do

A fair question to stop and ask — and one a sharp judge *will* ask: the rule
engine is ten lines of `if` statements, it's instant, it never hallucinates,
and this tutorial just told you it keeps final authority over every alert.
So why bring a neural network into a project that rules apparently already
solve?

The honest starting point is that for the crisp cases, the rules *are* the
better tool, which is exactly why we left them in charge. "Water above 35 cm
means flood warning" is a threshold; encoding a threshold as anything fancier
than an `if` statement is engineering theater. The AI earns its place in
everything around those crisp cases — the parts of the problem that resist
being written down as conditions at all.

**Rules only cover the situations someone thought of in advance.** Every
`if` clause is a situation a human enumerated. With three sensors this feels
manageable, but the space of *combinations* explodes: pressure falling
moderately while water rises slowly while temperature drops sharply — is
that one rule? Three? Does it need its own threshold? Add a fourth sensor
and the combinations multiply again. Rule engines grow by accretion into
hundreds of overlapping clauses that nobody fully understands (ask anyone
who maintains one professionally). A model doesn't enumerate — it learns the
*shape* of the decision from examples and interpolates between them. The 475
flashcards never contained pressure at exactly -2.7 hPa/hr with water at
exactly 24.3 cm, yet the model handles it, because it learned the pattern
rather than a lookup table.

**Rules can't recognize "this input makes no sense."** Look at what our
escalation examples teach: readings frozen for 30 minutes, water jumping
50 cm in 20 seconds, pressure screaming "storm" while water drains away —
and, from the firmware itself, `status: "ADC_ERROR"`. Each of these *could*
be a hand-written rule — but the list of ways sensors fail is effectively
endless, and each new rule only catches the one failure you already imagined.
What we trained instead is the *concept* of physical implausibility and of
distrusting bad hardware, from a handful of representative patterns.
Generalizing a concept from examples is precisely the thing statistical
learning does and hand-written logic doesn't. This is the model's most
genuine contribution to FloodGate: not replacing the thresholds, but
noticing when the inputs feeding those thresholds shouldn't be trusted.

**Rules output codes; models output explanations.** The rule engine can say
`issue_storm_watch`. The model says *why* — "barometric pressure falling
rapidly; storm likely approaching" — in the arguments of its tool call,
generated to fit the actual readings. For a public-facing warning system, the
explanation is half the product: a resident who reads a reason responds
differently than one who sees a red icon. And the same language interface
works in reverse — the system could ingest a free-text weather bulletin
tomorrow without anyone writing a parser, because language *is* the model's
input format.

**Rules have cliff edges; models can express doubt.** At 34.9 cm the rule
engine is perfectly calm; at 35.0 cm it's in crisis. Reality isn't like
that, and a system that can say "I'm not sure — get a second opinion" is
qualitatively different from one that can only flip between certainties. Our
`escalate_to_cloud` tool plus the confidence signal turns uncertainty into an
*action*, which enables the triage architecture: a free, instant, local
model handles the routine 99%, and only genuinely hard cases pay the cost of
a frontier model in the cloud. That routing pattern — small edge model as
intelligent filter for a large cloud model — is one of the most broadly
applicable ideas in this whole project.

**Changing rules means changing code; changing a model means changing
data.** To make the rule engine treat rapid temperature drops as a storm
signal, someone edits logic, and every edit risks breaking an unrelated
clause. To teach the model the same thing, you add examples to
`make_finetune_data.py` and retrain — twenty minutes, no logic touched, and
the eval harness immediately tells you whether anything else regressed. On a
two-person student team, "improving the system" becoming a data problem
instead of a code-surgery problem changes what you can attempt.

The transferable design rule for any similar project — plant monitors,
wildlife cameras, machine-health sensors, home safety devices — is a
three-layer split: **deterministic rules for whatever must never fail** (the
crisp, safety-critical thresholds), **a small local model for what can't be
enumerated** (pattern recognition, plausibility judgment, explanation,
uncertainty), and **a large cloud model only for what's rare and genuinely
hard**. Teams that skip the first layer build demos that fail unsafely;
teams that skip the second build brittle systems that shatter on the first
situation nobody predicted; teams that skip the third either overspend on
cloud calls or hit walls a small model can't climb. The interesting
engineering is in the seams — and that's exactly where FloodGate's
AGREE/DISAGREE comparison, confidence floor, and escalation logic live.

## 6. The textbook: `make_finetune_data.py` → `finetune_data.jsonl`

Out of the box, Needle 2 was trained on smart-home and phone actions —
"turn on the lights," "set a timer." It has never seen a storm drain.
Fine-tuning is how we teach it our vocabulary, and fine-tuning needs
examples.

`make_finetune_data.py` writes 475 of them into `finetune_data.jsonl`
(JSONL = one JSON object per line, the standard format for training data).
Each example is a complete flashcard with four parts: the **query** (a
sensor summary sentence, e.g. "pressure 1002.1 hPa, pressure trend
-4.5 hPa/hr, ... Decide the appropriate action."), the **tools** (the same
five-function menu the live system uses), the **answer** (the
exactly-correct tool call with exactly-correct arguments), and a short
**reasoning** trace ("Pressure falling at -4.5 hPa/hr, beyond the -2.0
threshold, while water is still safe. Storm watch."). The model trains on
the reasoning too — teaching it not just *what* to answer but *how to think
toward* the answer.

The 475 examples are deliberately distributed:

| Label | Count | What it teaches |
|---|---|---|
| all clear | 124 | stable pressure, safe water |
| storm watch | 113 | 90 by pressure trend + 20 by OpenWeather high-risk flag + 3 boundary |
| flood watch | 96 | level in [25, 35) cm or rising ≥ 0.5 cm/min |
| flood warning | 70 | level ≥ 35 cm |
| escalate | 70 | 40 adversarial (frozen/conflicting/spiking) + 30 hardware faults |
| boundary | ~15 | near-threshold cases labeled by the rule engine itself |

Real datasets are curated this way on purpose: you decide what situations
matter and make sure the model sees enough of each, including the weird
ones. Because the examples are synthetic (generated from the same threshold
math as the rule engine), the model is essentially being taught to imitate
the rules plus handle ambiguity and hardware faults. With real logged sensor
data from actual storms, the same file format works — you'd just fill the
flashcards from reality instead of from `random.uniform`.

One rule ties the whole project together: the query sentence format — field
order, units, wording — must be **byte-for-byte identical** in the training
data, in the live prompts, and in the evaluation. There is exactly one copy
of that format: `fg_core.build_prompt()`. The dataset generator also embeds
the generating feature dict in each row (`_features`) so
`make_finetune_data.py --check` can re-verify every query and label after
the fact, and it diffs the JSONL `tools` schemas against the live runtime's
`needle.build_schema()` output. Drift is structurally impossible.

## 7. The training: LoRA, and what `finetune_fp32.py` does

### What fine-tuning changes

Training a model from scratch means starting with random knobs and tuning
all 45 million of them on billions of words — months of GPU time.
Fine-tuning starts from the already-trained model and nudges it toward your
task with a few hundred examples — minutes on a laptop.

### What LoRA is

Even nudging all 45M parameters is wasteful, and it risks "catastrophic
forgetting" — overwriting what the model already knows. **LoRA (Low-Rank
Adaptation)** freezes the original weights entirely and learns a small
*correction* instead.

The idea: a weight matrix W in the network might be 512×512 = 262,144
numbers. Instead of changing W, LoRA learns two skinny matrices — A
(512×16) and B (16×512) — and the effective weight becomes W + A×B. That's
16,384 trainable numbers standing in for 262,144, about 6%. The number 16 is
the **rank** (hence "low-rank"). It works because the *change* needed to
adapt a model to a narrow task turns out to be simple — expressible in far
fewer dimensions than the full matrix.

Three practical bonuses: training is fast (fewer numbers to update), the
adapter is a small separate file (`floodgate_lora.pkl` — literally just the
A and B matrices for 10 weight groups), and you can't destroy the base model
because you never touched it. One clever initialization detail you can see
in the code: B starts as all zeros, so at step 1, A×B = 0 and the model
behaves exactly like the untouched base. Training grows the correction from
nothing.

### The training loop itself

`finetune_fp32.py` is a compact, complete example of how all neural network
training works. Each step: take a batch of 16 flashcards; run them through
the model to get its predicted next-token probabilities; compute the
**loss** (cross-entropy — a number measuring how surprised the model was by
the correct answer; lower is better); use **backpropagation**
(`jax.value_and_grad`) to compute, for every trainable number, which
direction would reduce the loss; and let the **optimizer** (AdamW) nudge
each number a tiny step (the learning rate, 0.0001) in that direction.
Repeat for 4 **epochs** (passes through all 475 examples). Our loss went
from ~2.5 to **1.50** — and then something instructive happened: we ran a
6-epoch version that reached **0.98** and the model got *worse* (held-out
accuracy dropped from 31% to 20% as it collapsed onto its favorite class,
storm watch). Exact-match accuracy is not monotonic in loss — the eval
harness, not the loss curve, is the arbiter. That's why `eval_model.py`
exists and why we always compare before shipping.

An important subtlety: the loss **mask**. Each flashcard contains both the
question and the answer, but we only want the model graded on the answer
tokens. The mask zeroes out the loss on the question part — the model
learns to *produce* answers, not to memorize questions.

### Why the "_fp32" in the name — our first war story

The vendor's own `needle finetune` command produced `loss nan` from step 2
onward. NaN ("not a number") is what floating-point math returns when a
calculation blows up — like dividing by zero — and once one NaN appears, it
infects everything it touches.

The clue was that step 1 was fine (2.58) and step 2 wasn't. Step 1 is the
untouched base model (remember, B starts at zero) — so the *first optimizer
update* was the poison. The cause: computers store decimals at different
precisions. The base model's weights are **float16** (half precision —
saves memory, but can only represent numbers down to about 0.00000006
before rounding to zero). The vendor's code created the LoRA matrices in
float16 too, and AdamW's stability constant, ε = 0.00000001, is *below*
float16's floor. It rounded to zero, the optimizer divided by zero, NaN
everywhere.

We proved the diagnosis by reproducing it in isolation — same optimizer math
on a synthetic toy matrix in float16, bfloat16, and float32. Float16: fine,
NaN, NaN, NaN. The other two: stable. That's the scientific method applied
to a bug: hypothesis, controlled experiment, confirmation. The fix in
`finetune_fp32.py`: keep the LoRA adapter and optimizer in float32 (full
precision) while the frozen base model stays float16, add gradient clipping
(a seatbelt against occasional huge updates), and refuse to save the adapter
if the loss ever goes non-finite — so a poisoned training run can never
silently produce a poisoned file again.

Lesson: **mixed precision is a real engineering discipline, not a
checkbox.** Even the vendor got it wrong.

### The version lock, learned the hard way

While iterating, pip moved on to cactus-needle **2.0.3**, and the results
were quietly worse: loss plateaued near 1.9 instead of 1.5, and the
trainer's LoRA target list shrank from 10 weight groups to 5 (it dropped
the model's multi-token prediction block). The `.cact` exporter also
changed. Nothing crashed — the model was simply *different and worse*, in a
way that only showed up in the accuracy numbers. The fix: **lock the library
versions that work.** The whole toolchain (cactus-needle 2.0.2, jax/jaxlib
0.11.0, flax 0.12.8, optax 0.2.8, numpy 2.5.2) is pinned in
`requirements-locked.txt`, and `pin_engine.py` refuses to run on any other
version. Lesson: in ML pipelines, "latest" is not a version — it's a moving
target that can silently change your results.

## 8. The shrink-wrap: `needle build` → `floodgate.cact`

The trained adapter isn't deployable by itself. `needle build` does two
things. First it **merges**: computes W + scale·A×B for each of the 10
adapted weight groups, producing one ordinary set of weights with the
fine-tuning baked in. Then it **quantizes**: converts most weights from
16-bit floats to low-bit codes. We build with `--bits 4` (4-bit weights /
8-bit activations): groups of weights share a tiny codebook of allowed
values and each weight stores only a small index into it.

We initially built with `--bits 2` and the tuned model measurably degraded —
it started producing "reasoning loops" that repeated the same phrase until
the token budget ran out and never emitted the tool call, scoring 5% on its
own training set. Rebuilding the *same* adapter at 4-bit fixed the loops and
tripled training-set accuracy. Lesson: **quantization is a hyperparameter,
not a checkbox.** A model whose output must be an exact JSON tool call is
more sensitive to compression than a model that writes prose, and the only
way to know your bit-width is safe is to measure accuracy on your own task,
not just load the file.

The result, `floodgate.cact`, is a ~23 MB self-contained blob — weights,
tokenizer, everything — that the C++ inference engine memory-maps and runs.
This single file is the deployment: copy it to the Raspberry Pi and point
`edge_brain.py` at it with `NEEDLE_WEIGHTS=floodgate.cact`. The Mac trains;
the Pi runs. The `.cact` doesn't care which CPU it lands on.

## 9. The detective tools: `diagnose_cact.py`, `pin_engine.py`, and the crash story

### The format skew

After fixing the NaN bug, training succeeded — and the exported model
*still refused to load*. `diagnose_cact.py` is the detective. Its key trick
is **isolating variables**: test the engine alone (pass), test loading the
vendor's own official model file (pass), test loading ours (fail). That
proves the engine works and can load external files — so something about
*our file specifically* is wrong. Then it parses the raw bytes of both
files. Every `.cact` starts with a **magic tag** — a fixed 4-byte number
that acts as a format version stamp. The official file: `0x05E12A83`. Ours:
`0x05E12A82`. One version apart.

Here's the kicker: every public exporter ≤ 2.0.2 writes the old `...82`
format, while the engine binary the package downloads only reads `...83`.
The vendor shipped a new engine before shipping the matching exporter. No
public install could produce a loadable file. This is called **version
skew**, and it's one of the most common failure modes in all of software —
two components, released on different schedules, silently disagreeing about
a shared format.

The workaround: the vendor's *older* 2.0.0 engine still reads our format,
and the Python package happens to prefer an engine file placed locally in
its own folder over the downloaded one. So we fetch the 2.0.0 engine and pin
it there — `pin_engine.py` repeats that on any machine (and `deploy_pi.sh`
does it automatically). It refuses to run on any cactus-needle version other
than the locked 2.0.2.

Lesson: when a system spans multiple components, **binary formats are
contracts**, and you debug contract disputes by finding a known-good example
(the official file) and diffing against it at the byte level. The schema
contract in section 3 and this format contract are the same species of bug
at different layers — two things that must agree, silently drifting apart.

### The engine segfault, and crash-isolated workers

While evaluating the tuned model, the whole Python process started dying
with `Segmentation fault` after ~6-8 inferences. Isolation tests pinned it
down: the needle engine dylibs (both 2.0.0 and 2.0.3, on macOS) segfault
after a handful of `complete()` calls **when external `.cact` weights are
loaded** — the baked-in base model survived 100+ calls. Bigger buffers
didn't help, neither did recreating the model object: it's memory corruption
in the engine's external-weights path.

A segfault kills the entire process, which would take down the brain
mid-demo and mid-eval. The fix is a classic systems pattern: **isolate the
untrustworthy component in a child process and supervise it.** Inference now
runs in `infer_worker.py`, a tiny subprocess that loads the model once and
answers one JSON prompt per line. The parent (`edge_brain.InferenceClient`)
writes a prompt, reads the answer, and if the worker dies (no output on
stdout), it restarts the worker and retries — twice — before giving up and
letting the rule engine run the cycle alone. The decision loop can never be
killed by the model library again, on any platform. On the Raspberry Pi the
Linux engine build is probably fine, but the worker costs nothing and makes
the service genuinely robust either way.

Two lessons. First, **never let a third-party native library run in your
main process when its reliability is unknown** — a supervised worker turns a
fatal crash into a log line and a retry. Second, our job is to make the
system survive even where the vendor's bug does. That's the difference
between a demo and a deployment.

## 10. Knowing whether it's actually good: `eval_model.py`

"It loads and it ran once" is not evidence. `eval_model.py` generates 200
*fresh* scenarios — same physics, new random values, including
near-threshold cases the training set never contained — asks the model to
decide each one (through the crash-isolated worker, of course), and grades
it against the rule engine as ground truth. The scenario set includes
**sensor-fault scenarios** (`ADC_ERROR` / `STALE`), which the rule engine
itself answers with `escalate_to_cloud`, plus a separate **adversarial
check**: frozen, conflicting, and spiking readings that *fool* the rules,
where the model should escalate anyway. It reports per-class accuracy, a
**confusion matrix** (a grid showing, for each true situation, what the
model predicted — so you can see not just how often it's wrong but *which*
mistakes it makes), and latency.

Two ideas here matter far beyond this project. **Held-out evaluation**:
never grade a model on the exact examples it trained on — that measures
memorization, not learning. **Not all errors are equal**: confusing
flood_watch with flood_warning at the 35 cm boundary is harmless; calling a
real flood "all clear" is dangerous. The confusion matrix is how you see
the difference. Running the eval twice — once with `--weights floodgate.cact`,
once without — on the same seeded scenario set gives the before/after number
that proves the fine-tuning did something: our measured numbers are 16% base
→ 31% tuned, with `report_all_clear` going 0% → 85%.

## 11. The conductor: `setup_mac.sh`, `deploy_pi.sh`, and the remaining files

`setup_mac.sh` chains the whole Mac pipeline — environment (locked
toolchain), dataset generation + consistency check, training, build, smoke
test, packaging — so one command reproduces everything from scratch. That's
**reproducibility**, and it's the difference between "it worked once on my
laptop" and engineering.

The Pi side got the same treatment: `deploy_pi.sh` is a one-shot
provisioning script for a **Raspberry Pi 4 or 5** running 64-bit Raspberry
Pi OS. It checks the architecture, creates a venv, installs the
**inference-only** dependencies (no jax — the Pi doesn't train), pins the
engine, evaluates the tuned model *on the Pi*, runs a 60-second simulator
smoke test, and installs a **systemd service** (`floodgate-brain.service`)
that runs `edge_brain.py --mqtt` with the tuned model and auto-restarts if
it crashes. A systemd unit is the standard way to make a Python program
behave like a proper always-on service: it starts at boot, restarts on
failure, and logs to `journalctl -u floodgate-brain -f`. It coexists with
the main project's bridge and web-dashboard services (the brain's API is on
`:8090` so it never collides with the dashboard on `:8080`). Pi 4 note:
inference is ~2-3× slower than a Pi 5 (Cortex-A72 vs A76) — expect roughly
150-350 ms per decision, well inside the 10-second decision period.

The remaining folder items: `fg_core.py` (the single source of truth — read
it first), `infer_worker.py` (the crash-isolated inference worker),
`checkpoints/` holds the downloaded base model (`needle2.pkl`, the starting
point for training), `floodgate_lora.pkl` is your trained adapter (keep it —
retraining is the only way to recreate it), `floodgate.cact` is the
deployable, `finetune_data.jsonl` is the training data, `esp32_sender.ino`
is the HTTP alternative sender (same payload schema as the main firmware),
and `__pycache__/` is Python's disposable compiled-file cache. Remember: the
toolchain is deliberately **locked** — don't "upgrade" it.

## 12. What you can honestly claim, and what you can't

You can say: we designed an end-to-end flood detection system whose
decision-making is a three-layer stack — auditable deterministic rules that
retain alert authority, a 45M-parameter model we fine-tuned on 475 domain
examples using LoRA on a laptop (loss 2.5 → 1.50), and an optional cloud
tier for rare, genuinely hard cases. The tuned model is deployed as a
~23 MB 4-bit quantized binary making sub-second decisions fully offline on a
Raspberry Pi 4; it consumes the same MQTT payload as the rest of the system;
the AI is checked against the rules every cycle (base 16% → tuned 31%
held-out agreement, with calm conditions at 85%); sensor faults and
ambiguous readings escalate to a second opinion. Along the way we diagnosed
and fixed a half-precision numerical-stability bug in the vendor's trainer,
worked around a format version skew by binary-diffing the deployment format,
proved that 2-bit quantization silently breaks this task, isolated a macOS
engine segfault behind a crash-restartable worker, and locked the toolchain
after discovering a silent change in the trainer's target layers.

Be equally clear about limits: the model was trained on synthetic data
generated from the same rules it's compared against, so agreement partly
reflects imitation; the water-threshold classes are beyond what this 45M
model reliably learns, and the deterministic rule engine is what actually
protects people; the honest next step is retraining on real logged storm
data. The dashboard's risk engine grades in meters while the edge brain
grades in centimeters (25/35 cm vs 0.15/0.8 m) — the mapping was never
reconciled against real drain geometry. Judges consistently reward teams
who know their system's boundaries over teams who oversell.

## Glossary

**Parameter/weight** — a learned number inside a neural network. **Token** —
a chunk of text (roughly a short word) that models read and write one at a
time. **Loss** — a single number measuring how wrong the model's predictions
are; training exists to push it down. **Gradient** — for each parameter, the
direction and steepness of loss change; computed by backpropagation.
**Optimizer (AdamW)** — the algorithm that turns gradients into actual
parameter updates. **Learning rate** — the step size of those updates.
**Epoch** — one full pass through the training data. **LoRA** — fine-tuning
by learning small low-rank correction matrices while freezing the base
model. **Rank** — the inner dimension of those corrections; ours is 16.
**float16/float32** — 2-byte vs 4-byte decimal storage; the tradeoff is
memory vs numerical range. **NaN** — the value floating-point math produces
when a computation blows up. **Quantization** — compressing weights to very
few bits (ours: 4) to shrink and speed up a model. **Tool calling** — a
model whose output is a structured function call rather than prose.
**Inference** — running a trained model to get answers (vs training).
**Edge AI** — running models on small local devices instead of cloud
servers. **MQTT** — a lightweight pub/sub messaging protocol; the ESP32
publishes sensor JSON to a broker and the Pi services subscribe to the same
topic. **Single source of truth** — the one module (`fg_core.py`) that owns
every value or string that must be identical across training, runtime, and
evaluation. **Confusion matrix** — a grid of true situation vs predicted
answer, revealing which mistakes a model makes. **Version skew** — failures
caused by two components being on incompatible versions. **Magic tag** — a
fixed byte sequence at the start of a file identifying its format version.
**Held-out evaluation** — grading a model on scenarios it never trained on,
to measure learning rather than memorization.
