# SODA system

A personal AI operating system that **learns its owner's rules from one sentence of feedback**, asks before it acts on
their behalf, and maintains itself. Built and run daily by Alessandro Sodano; this repository is the engine with
**no data in it**: every ledger, rule, note and card under `examples/` is invented, so you can start from them and
replace them with your own.

It is tailored for **Postgres 16 + pgvector** (one store for notes, rules, people, cards, to-dos and events, with
embeddings and hybrid search) and for the **Claude Code CLI** as the model runtime (`claude -p`, no API keys in code).

## In daily use

Figures read from the live system on 7 Oct 2026 (counts only; none of the underlying data is in this repository):

- in use since July 2026 (first commit of the task repository: 30 Jul 2026);
- approximately **320 rules** filed in the owner's own words across **10 rule ledgers**: hub 80, email 99, CRM 62,
  proactive 28, travel 21, notifications 8, meetings 8, cleaning 7, board 4, whitelist 3;
- approximately **22,000 rule-hit lines** logged by the observers;
- approximately **45 decisions a day** on the approval hub (637 in the 13 days since the decision log started on
  24 Sep 2026);
- approximately **20 always-on services and 30 scheduled jobs** across an always-on server and a laptop.

## The idea in one picture

```
                        +-------------------- the owner (phone: Telegram, review page) -------------------+
                        |  "3 yes"     "3 no: too long"     "notif: never show me reactions"     "rule: ..." |
                        +-----^------------------|----------------------------|-----------------------------+
                              | card #N          | verdict + reason           | feedback sentence
                              |                  v                            v
  producers               +---+----------------------------------------------------------+
  - drafting sessions --->|  APPROVAL HUB  hub/server.js                                  |--- yes ---> executor (argv, no shell)
  - notification layer -->|  guard (owner's filed patterns) . one ping per subject        |            the ONLY path to a person
  - system agent -------->|  decisions.jsonl . feedback queue                             |
  - feedback session ---->+-----------------------------------------+---------------------+
         ^                                                          | queue
         | reads the COMPILED SKILL                                 v
         | on every call                        +-------------------+--------------------+
  +------+---------+     compile_skill.py       |  FEEDBACK SESSION  rules/               |
  |  SKILLS        |<-------------------------- |  ledger_verdict: like / confirmation /  |
  |  one screen    |   counts from              |  rule / case / system / work            |
  |  per surface   |   rule-hits.jsonl          |  -> addrule.py -> the ledger it is ABOUT|
  +----------------+          ^                 +-------------------+--------------------+
                              | hit lines                           |
                  observers: hub guard, notif-observer,             v
                  critics (one JSON line per rule checked)   LEDGERS *-CONTRACT.md
                                                             verbatim, dated, append-only
                                                                    |
                                                                    v  indexed every few minutes
                                    +---------------------------------------------------------------+
                                    |  THE BRAIN  Postgres + pgvector  brain/                         |
                                    |  pages . chunks . rules . people . cards . to-dos . events      |
                                    |  /brain/recall  /brain/match  /brain/rules  MCP /mcp            |
                                    +---------------------------------------------------------------+
                                    system agent: checks -> whitelisted fixes -> ONE "STUCK" card
                                    sim/: the whole loop in a sandbox, simulated world + owner, scored per day
```

## The loops

| | Loop | What it shows | Doc |
|---|---|---|---|
| 1 | **The rule loop** | Feedback typed anywhere becomes a verbatim row in a per-surface ledger, the ledger is compiled into a one-screen skill that every decider reads before acting, observers log rule hits, and the hit counts decide which rules stay in front. | [docs/01-rule-loop.md](docs/01-rule-loop.md) |
| 2 | **The approval hub** | Every outbound action is a card answered from the phone; the hub runs the action on yes through a whitelisted executor. A guard filters cards with patterns filed from the owner's own sentences. | [docs/02-approval-hub.md](docs/02-approval-hub.md) |
| 3 | **The brain** | One Postgres + pgvector store over notes, rules, people, cards, to-dos and events; hybrid vector + full-text search; `/brain/match` finds the open to-do, person or card an event is about. | [docs/03-brain.md](docs/03-brain.md) |
| 4 | **Notifications** | Incoming messages are pooled, filtered in code (already answered, stale), classified by a model that reads the compiled notification skill on every batch, and collapsed in place once answered. | [docs/04-notifications.md](docs/04-notifications.md) |
| 5 | **The system agent** | Monitors, whitelisted self-fixes with caps, a two-pass proof, a registry of what is broken, and one "STUCK" card whose yes performs the fix. | [docs/05-system-agent.md](docs/05-system-agent.md) |
| 6 | **The simulation harness** | A world agent plays the contacts, an owner agent with a persona answers cards and gives feedback, the real hub and rules engine run in a sandbox over simulated days, a judge scores every event, and arms (no brain / brain / no learning) are compared with a verdict. | [docs/06-simulation.md](docs/06-simulation.md) |

## Repository layout

```
config/surfaces.json     the ledgers and where feedback comes from (the one place to add a surface)
lib/                     soda_paths.py / soda_env.js: every path from the environment, .env loading
rules/                   the rule loop: addrule, compile_skill, ledger_verdict, feedback_session, hits, rule_stats, llm
hub/                     the approval hub (server.js), its review page, the Telegram reply poller, example guard + executors
brain/                   schema.sql (+ migration), indexer, HTTP + MCP server, to-do / card ingest, event matcher
notif/                   the notification daemon, debounce, filters, observer, collapse, Telegram push
system-agent/            checks, registry, fix guard, unstuck, example config
sim/                     simulation harness: world / owner / producer agents, sandbox clock, judge, compare, arms, scenario
examples/                invented ledgers, skill maps, compiled skills, hit lines, brain pages
docs/                    one page per loop
scripts/quickstart.sh    data dir from the examples + compile every skill
tests/                   end-to-end tests of the rule loop and the simulation (offline, model stubbed)
```

State never lives in the repo: everything the system writes goes to `$SODA_HOME` (default `./data`, gitignored).

## Quickstart

Requirements: Python 3.10+, Node 18+. For the model calls: the [Claude Code](https://docs.anthropic.com/en/docs/claude-code)
CLI, logged in (`claude -p` must work). Without it, set `SODA_LLM_STUB` to a file and every model call is answered
from that file. For the brain: Postgres 16 with pgvector.

**1. Create the data directory from the examples and compile the skills**

```bash
./scripts/quickstart.sh
# ledger    ./data/ledgers/HUB-CARD-CONTRACT.md ...
# wrote data/skills/hub/SKILL.md; 45 lines ...
cat data/skills/hub/SKILL.md
```

**2. Run the hub and post a test card**

```bash
node hub/server.js &                       # 127.0.0.1:4180; without a Telegram token cards are printed here
curl -s 127.0.0.1:4180/pending -H 'content-type: application/json' \
  -d '{"text":"Say hello?","kind":"decision","action":{"type":"echo","text":"hello"}}'
# {"ok":true,"id":"a...","seq":1,"guard":"ok"}
open http://127.0.0.1:4180                 # the review page: Yes / No / "why"
```

Approve from the shell instead: `curl -s 127.0.0.1:4180/resolve -H 'content-type: application/json' -d '{"id":"<id>","verdict":"yes"}'`.
The `echo` executor runs and the result appears on the card.

**3. Teach it a rule**

Reject a card with a reason (on the page, or `{"verdict":"no","feedback":"never bundle two people in one card"}`),
then run the feedback session:

```bash
python3 rules/feedback_session.py --dry    # what it would file
python3 rules/feedback_session.py          # files it, recompiles the skill, posts one "told" update card
tail -n 3 data/ledgers/HUB-CARD-CONTRACT.md
```

Or file one by hand: `python3 rules/addrule.py --contract hub --rule "..." --quote "..."`, then
`python3 rules/compile_skill.py --contract hub --install`.

**4. Telegram (optional)**: set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, restart the hub, and run
`node hub/tg-replies.js` to answer cards with `3 yes` / `3 no: why` from the phone.

**5. The brain (Postgres + pgvector)**

```bash
sudo apt install postgresql-16 postgresql-16-pgvector
sudo -u postgres psql -c "CREATE ROLE soda LOGIN PASSWORD 'change-me'" -c "CREATE DATABASE soda OWNER soda"
sudo -u postgres psql -d soda -c "CREATE EXTENSION IF NOT EXISTS vector"
psql "postgresql://soda:change-me@127.0.0.1/soda" -v ON_ERROR_STOP=1 -f brain/schema.sql
python3 -m venv ~/.venvs/soda-brain && ~/.venvs/soda-brain/bin/pip install -r brain/requirements.txt
~/.venvs/soda-brain/bin/python brain/brain_index.py      # indexes examples/brain + your ledgers
~/.venvs/soda-brain/bin/python brain/brain_serve.py      # 127.0.0.1:4150, HTTP + MCP at /mcp
curl -s 127.0.0.1:4150/brain/recall -H 'content-type: application/json' -d '{"query":"how long should a card be"}'
curl -s 127.0.0.1:4150/brain/rules  -H 'content-type: application/json' -d '{"topic":"notifications"}'
```

`schema.sql` is idempotent; apply it on every deploy. Details, the tables and every route: [docs/03-brain.md](docs/03-brain.md).

**6. Notifications and the system agent**: [docs/04-notifications.md](docs/04-notifications.md),
[docs/05-system-agent.md](docs/05-system-agent.md).

**7. Simulate before you change anything live**

```bash
python3 sim/run.py --arm A --days 6 --run t-A --stub      # offline and deterministic; drop --stub for model agents
python3 sim/run.py --arm B --days 6 --run t-B --stub
python3 sim/compare.py t-A t-B                            # curves per arm + VERDICT with delta and p-value
```

See [sim/README.md](sim/README.md) for arms, scenarios, Langfuse tracing and the honest limits.

**Tests**

```bash
python3 tests/test_rule_loop.py            # hub + guard + idem + executor + rejection -> rule -> recompiled skill
python3 tests/test_sim.py                  # 2-day stub simulation, arms A and B: a day-1 rule is read on day 2; verdict
(cd notif && npm test)                     # debounce, filters, observer hit lines, collapse
(cd system-agent && python3 test_registry.py && python3 test_fix_guard.py)
```

## Make it yours

1. Edit the example ledgers in `data/ledgers/` (or empty their tables and let your own feedback fill them).
2. Keep `data/skill-maps/` in step: merge new rows into compiled rules when you have a minute. Until you do, a new HARD
   row compiles on its own, verbatim, in front.
3. Add a surface: one entry in `config/surfaces.json`, a ledger, a skill map, and make the code that decides read the
   compiled skill (the six-part template in [docs/01-rule-loop.md](docs/01-rule-loop.md)).
4. Point `SODA_SKILLS_DIR` at `~/.claude/skills` so Claude Code sessions load the compiled skills as skills.
5. Write executors for your own senders in `data/hub/executors.json`: argv only, no shell, as few as possible.

## What is not here

The private deployment also has a CRM application, channel adapters (mail, chat, calendar, meeting notes), a daily
planning page and travel search. They are left out because they are specific to one person's
accounts; the interfaces they plug into (the hub's `/pending` and `/feedback`, the notification store line format,
the brain's `/crm/put` and `/crm/events`) are documented here.

## License

Apache-2.0. See [LICENSE](LICENSE). Security notes: [SECURITY.md](SECURITY.md).
