# 6. The simulation harness

Changing a system that acts on someone's behalf is risky to test live: a bad producer sends a bad email to a real
person. The harness runs the REAL system in a sandbox against a simulated world and a simulated owner, for as many
simulated days as you like, and scores every day against the world's hidden truth, so two setups can be compared on
the same events.

```
            sim/run.py --arm B --days 5 --run trial-B                      $SODA_HOME/sim/trial-B/  (the sandbox)
 ------------------------------------------------------------------------------------------------------------------
 sandbox clock    day 1 ............................................. day 2 ............ day N   settle morning
                  |                                                    |
 WORLD agent      events (each with a hidden truth: needs an answer? which person? which step?)
                  |
 at each pass     PRODUCERS (the system under test)                     read skills/ FROM DISK every pass
 09:00 13:00 18:00  match person (brain off | local | door) -> step -> draft -> POST /pending
                  |                                                    |
 owner passes     OWNER agent (persona) answers every open card:        hub/server.js (real, sandbox port,
 13:00 18:00        yes -> the hub runs sim/outbox.py (the only "send") no Telegram)
                    no + one sentence -> the hub's feedback queue
                  |
 end of day       rules/feedback_session.py (real) -> ledger_verdict -> addrule -> compile_skill --install
 (arm learns)       => tomorrow's producers read a different skill
                  |
 end of run       sim/judge.py scores every event of every day -> scores.jsonl, result.json
                  sim/compare.py <runs> -> curves per arm + VERDICT with delta and permutation p
 observability    trace.jsonl always; Langfuse when its keys are set (trace per day, spans, generations)
```

## The parties

| Agent | Stub mode (offline, deterministic) | Model mode (`claude -p` via `rules/llm.py`) |
|---|---|---|
| World | seeded templates over the cast; a share of mail from an address the CRM does not know; a share of noise | writes the day's mail for the cast, with the truth per event |
| Producer (system under test) | matches, picks the step, drafts; follows a rule only when it finds the rule in its compiled skill | the skills are in its prompt, it answers `{actionable, step, subject, body}` |
| Owner | knows the truth, checks the scenario's preferences, says each problem once in its own words | the persona (`persona.md`) and the card, answers `{verdict, feedback}` |

The owner's taste is in `scenario.json` as checks (`sim/checks.py`), shared by the stub owner and the judge. The
system under test never imports them: it can only learn the taste through the owner's sentences, filed by the real
`rules/` engine and read back as compiled skills. That separation is what makes a learning curve mean something.

## The eval

Per event that needed an answer (100 points):

| Component | Points | Means |
|---|---|---|
| detected | 20 | a card exists for the event |
| person | 10 | the card names the right person |
| timely | 10 | the first card came no later than the deadline day |
| step | 30 | the proposed step is the true one (reply / schedule / send-doc) |
| artifacts | 20 | share of the owner's preferences the decisive draft satisfies |
| clean | 10 | one person per card, at most one redo |

A send without the owner's yes scores the event 0. An event that needed no answer scores 100 when no card was made and
0 when one was. The day score is the mean over the day's events. Scoring is code, not a model, so the same day always
gets the same score.

**Arms** are named configs (`sim/arms/*.json`): A = no brain, B = brain on, C = rules learning off. `compare.py`
prints each arm's curve over the days, the component shares, and per arm a verdict against the base: the difference
of mean per-event scores and a two-sided permutation p-value (5000 seeded shuffles), with the wording "no clear
difference" / "a hint" / "a real difference in this simulated world".

## What a run shows (stub, seed 7, 6 days)

Learning on: three owner sentences become three rules on days 1 to 3 (one hub rule, two email rules); the share of
drafts that satisfy the owner's taste goes from 0 to 1 and approvals start on day 4, the first day after the third rule. Learning off: the share stays 0
and nothing is ever approved (C is 14 points per event below A, p < 0.001). Brain on fixes the person for mail from
unknown addresses (person share 1.00 vs 0.74), which is visible in the components but not significant over 36 events.
The commands and the full output are in [sim/README.md](../sim/README.md).

## Limits

The stub agents are code, the classifier is stubbed in stub runs, the judge only knows the scenario's taste, the
brain is a token-overlap stand-in unless pointed at a real door, there are no outages or sync delays, and n is small.
The full list is in [sim/README.md](../sim/README.md#the-honest-limits). A simulation shows that a setup helps in
this world; only live numbers show it helps in real life.
