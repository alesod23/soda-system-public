# sim/: test a system setup without touching real life

A simulated world (a fictional cast at `example.com`), a simulated owner with a persona, and the REAL system in a
sandbox: the real `hub/server.js`, the real `rules/` engine (addrule, compile_skill, ledger_verdict,
feedback_session). Run one setup ("arm") for N simulated days, run another on the same seed, and compare.

Design and scoring: [docs/06-simulation.md](../docs/06-simulation.md).

## Run an arm

```bash
python3 sim/run.py --arm B --days 5 --run trial-B --stub          # offline, deterministic, no model calls
python3 sim/run.py --arm B --days 5 --run trial-B-model            # world, producers and owner on `claude -p`
```

- `--arm A|B|C` or a path to your own arm JSON (`sim/arms/`): A = no brain, B = brain on, C = brain on with rules
  learning off. Add an arm by copying one: it is a named config the runner reads (`brain`: off | local | door,
  `learning`: true | false).
- `--seed` (default 7) fixes the world's events: arms compared on the same seed saw the same day.
- `--scenario` (default `sim/scenario/example`): `cast.json` (the contacts; `alt_email` is an address the CRM does
  not know), `persona.md` (what the model owner is told), `scenario.json` (passes, event mix, the owner's
  preferences), and the seed `ledgers/` + `skill-maps/` the run starts from.
- `brain: door` with `BRAIN_URL=http://127.0.0.1:4150` asks a running brain (`POST /brain/match`) instead of the
  local stand-in.
- Langfuse: set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` (and `LANGFUSE_HOST`) and install the `langfuse` Python
  SDK: one trace per simulated day, spans per pass, event and owner decision, a generation per model call. Without
  them it is a no-op; every span is always in `<run>/trace.jsonl`.

The sandbox is `$SODA_HOME/sim/<run>/`: `ledgers/`, `skills/` (compiled), `skills-read/day<N>/` (exactly what the
producers read that day), `hub/state.json`, `decisions.jsonl`, `feedback/`, `outbox.jsonl` (the only "sends"),
`events.jsonl` (with the hidden truth), `scores.jsonl`, `trace.jsonl`, `result.json`.

## Compare arms

```bash
python3 sim/run.py --arm A --days 6 --run t-A --stub
python3 sim/run.py --arm B --days 6 --run t-B --stub
python3 sim/run.py --arm C --days 6 --run t-C --stub
python3 sim/compare.py t-A t-B t-C
```

Output of exactly these commands (stub, seed 7):

```
score per simulated day (0-100, mean over that day's events)

           d1     d2     d3     d4     d5     d6      mean
A (t-A)     88.3   78.3   96.7  100.0  100.0   95.0   93.0
B (t-B)     90.0   88.3   96.7  100.0  100.0  100.0   95.8
C (t-C)     86.7   71.7   80.0   81.7   76.7   76.7   78.9

components (share of actionable events, all days)

           detected    person    timely      step artifacts     clean   rules filed   sends
A (t-A)        1.00      0.74      1.00      0.96      0.72      1.00             3      16
B (t-B)        1.00      1.00      1.00      1.00      0.72      1.00             3      16
C (t-C)        1.00      1.00      1.00      1.00      0.00      0.19             0       0

VERDICT B vs A: +2.8 points per event (95.8 vs 93.1, n=36 vs 36, days >= 1), permutation p=0.314: no clear difference; ...
VERDICT C vs A: -14.2 points per event (78.9 vs 93.1, n=36 vs 36, days >= 1), permutation p=0.000: a real difference in this simulated world (worse); ...
```

How to read it: with learning on (A, B), the `artifacts` share climbs from 0 to 1 as the owner's three sentences
become rules (days 1 to 3) and the drafts start passing; with learning off (C) it stays at 0 and nothing is ever
approved. The brain (B) fixes person matching for mail from unknown addresses (A's `person` 0.74), which also fixes
the step when the right step depends on what is open with that person. B's gain over A is real in the components but
not significant over 36 events: run more days or seeds before believing it.

## The honest limits

- **The stub agents are code.** The stub producer "reads" its skill by looking for a few phrasings of the scenario's
  rules; the stub world draws from templates; the stub owner knows the truth. A stub run tests the plumbing and the
  loop (a sentence on day 1 is a rule in the skill read on day 2), not model behaviour. Model runs test behaviour,
  cost money and are not reproducible.
- **The classifier is stubbed in stub runs.** The owner's sentence goes through the real hub, queue, feedback
  session, addrule and compile, but `ledger_verdict`'s model call is answered from a map the owner wrote. In a model
  run the real classifier decides, and may file differently.
- **The judge measures the scenario's taste, nothing else.** Three preferences, six components, fixed weights. A
  setup can win here and lose on something the scenario never asks about.
- **One machine, one clock, no outages.** Passes are discrete; latency, sync, sleeping laptops and expired sessions
  do not exist here unless you script them.
- **The brain is a stand-in** unless `brain: door` points at a real one: token overlap over the cast, which is easy
  for a four-person cast.
- **Small n.** A few dozen events per arm; the permutation p-value is a guide, not a proof. A simulation can show a
  setup helps in THIS world; only live numbers show it helps in real life.
