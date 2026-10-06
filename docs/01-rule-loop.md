# 1. The rule loop

The design goal, in one sentence: **the owner says "this is bad because X" once, wherever they are looking, and the
system never makes that mistake again, without the owner ever opening a file.**

Everything else in this repository (the hub, the notifications, the system agent, the brain) is a *surface* of this
loop: a place where the system makes an artifact, the owner judges it, and the judgement becomes a rule.

```
   what happens                the memory                    the work                     the owner
   ------------                ----------                    --------                     ---------
   events --> [observe] --> STATE per thing --> [generate] --> artifact --> [observe] --> sees it on the phone / page
                                  ^              reads the                     |                 |
                                  |              COMPILED SKILL                | hit lines       | "this sucks because X"
                                  |                   ^                        v                 v
                                  |                   |               rule-hits.jsonl     hub POST /feedback
                                  |                   |                        |                 |
                                  |             compile_skill.py <-- counts ---+                 v
                                  |                   ^                                  feedback_session.py
                                  |                   |                                  ledger_verdict.py
                                  +----------- LEDGER (*-CONTRACT.md) <--- addrule.py <--------+
                                               verbatim, dated, append-only
```

## The parts

| Part | File | What it is |
|---|---|---|
| **Ledger** | `$SODA_HOME/ledgers/<NAME>-CONTRACT.md` | Every sentence the owner ever gave about one kind of artifact: dated, verbatim, append-only. `## HARD` and `## SOFT` tables of `\| H<n> \| rule *"their words"* [item: id] \| source \|` rows. Rule ids never change. A `LIKED:` row is a positive example. See `examples/ledgers/`. |
| **Capture** | `rules/addrule.py` | One call files one sentence as the next `H<n>` row (`--soft`, `--like`, `--maps-to`, `--item`). For the hub, `--suppress <regex>` also writes a live filter the hub applies on the next card. |
| **Skill map** | `$SODA_HOME/skill-maps/skill-map-<key>.json` | Hand-kept: which ledger rows merge into which compiled rule, its group, one example, and whether it starts in front. A row nobody mapped yet still compiles, verbatim. See `examples/skill-maps/`. |
| **Compile** | `rules/compile_skill.py` | Ledger + map + hit counts -> one screen of rules (`SKILL.md`, at most `skill_cap` in front, at most 110 lines). Self-checks: every `H<n>` exactly once, no dashes, no emoji, cap respected. Exit 1 writes nothing. See `examples/skills/`. |
| **Decider** | whatever produces the artifact | Reads the compiled skill on EVERY call (the notification classifier, the sessions that post hub cards or draft emails). Never the raw ledger: the ledger grows forever, the skill stays one screen. |
| **Observer** | `rules/hits.py` (python), `notif/notif-observer.js` (node), the hub's guard | After the fact, checks the artifact against the rules and appends one hit line. Never raises, never blocks, never edits. |
| **Feedback session** | `rules/feedback_session.py` + `rules/ledger_verdict.py` | The only consumer of the feedback queue. Classifies each sentence and files it in the ledger it is ABOUT, then recompiles. |
| **Stats** | `rules/rule_stats.py` | Which rules fire, which are never checked, which are checked often and never violated (dead weight in every prompt). |
| **Brain index** | `brain/brain_index.py` | Every ledger row is also a page of the Postgres brain, so `POST /brain/rules {topic}` answers "what does the owner like and dislike about X" in their own words. See [03-brain.md](03-brain.md). |

## The hit line

Every observer writes the same shape, one JSON object per line, to `$SODA_HOME/rule-hits.jsonl`:

```json
{"ts":"2026-01-22T08:05:00Z","surface":"notif","rule":"H1","action":"ok","item":"n20260122T080500",
 "model":"opus","skill_v":"2026-01-19a","path":"notif chat","by":"notif-observer.js","note":"2 lines, each names the sender"}
```

- `surface` is the LEDGER the rule belongs to (the compiler counts by it); `by` names the observer.
- `action: "ok"` = checked and held. Anything else (`flag`, `regen`, `downgrade`, `block`) is a hit.
- `skill_v` says which compile the artifact was judged against, so a new compile shows up as a step in the numbers.
- Items whose id has a `zz-` or `test` segment are fixtures and never count (`rules/hit_filter.py`).

## How the counts move rules

`compile_skill.py` reads the hits per compiled rule:

- a rule that fired at all, with fewer than `retire_after_applicable` (50) observed artifacts, comes or stays in front;
- a rule with 0 hits over the last 50 artifacts where it applied sinks to **Assumed** (still checked by the observer,
  no longer spending prompt space), and comes back in front the moment it fires;
- an untouched rule keeps its mapping's `front` flag;
- a fresh HARD row nobody has mapped yet outranks everything: the newest feedback is in front at once.

The example skills in `examples/skills/` were compiled with `examples/hits/rule-hits.example.jsonl`: the hub's "one
card, one decision" rule starts in Assumed in its map, fired twice in the example hits, and is therefore in front.

The ladder for a rule that keeps firing: in the skill with an example -> pinned with a second example ("this again") ->
a deterministic check in code -> the check asks the owner keep-or-drop.

## The feedback session: six classes

Every queued sentence is classified by one model call (`rules/ledger_verdict.py`, the prompt is in the file) and
filed in code:

| Class | Example sentence | Where it goes |
|---|---|---|
| `like` | "perfect, exactly this tone" | a `LIKED:` row; with `maps_to` it becomes that rule's example |
| `confirmation` | "again: say who wrote it" | one line in `rule-confirmations.jsonl` against the existing rule, no duplicate row |
| `rule` | "a group message that only announces an event is never a notification" | a new `H<n>` row in the ledger it is ABOUT; skill recompiled |
| `case` | "this one I had already seen" | one line in `rule-cases.jsonl` (the observer's example set), never a row |
| `system` | "add the sender's company to the card" | `feedback/handoffs.jsonl`, for a working session with tools |
| `work` | "draft a reply to her" | `feedback/handoffs.jsonl` |

**Which ledger.** The owner never picks. The place the sentence was typed gives a default (`config/surfaces.json`,
`surface_ledger`), and the classifier moves it to the ledger it is actually about: a comment on a notification card
about how the suggested reply is worded is an `email` rule, not a `notif` rule. One sentence about two things is two
entries.

**Declared rules.** A sentence typed as `rule: ...` is filed as a rule even when the classifier reads it as a case.

**The told card.** Each run posts ONE update card to the hub ("feedback filed: 3 sentences" with where each went), so a
mis-filed rule is corrected with one more sentence on that card, which comes back through the same loop.

## The template of a loop (adding a surface)

Every surface where the owner looks at something the system made gets the same six parts. A surface missing one does
not learn.

| # | Part | How a new surface gets it |
|---|---|---|
| 1 | Ledger | copy an example ledger's header into `$SODA_HOME/ledgers/<NAME>-CONTRACT.md`; add the key to `config/surfaces.json` `ledgers` |
| 2 | Skill, read by the decider | write `skill-map-<key>.json` (copy an example), run `compile_skill.py --contract <key> --install`, and make the code that DECIDES read `$SODA_SKILLS_DIR/<name>/SKILL.md` on every call |
| 3 | Input box on the surface | anything that POSTs `{surface, item, line, text}` to the hub's `/feedback`: the review page, a Telegram prefix (`hub/tg-replies.js`), a one-line form on any page |
| 4 | Route | one line in `config/surfaces.json` `surface_ledger` (surface -> default ledger) |
| 5 | Observer | a check in the code that produces the artifact, writing hit lines through `rules/hits.py` (or the same JSON from any language) |
| 6 | Brain index | nothing: `brain_index.py` indexes every `*-CONTRACT.md` on its next pass |

## Commands

```bash
python rules/addrule.py --contract hub --rule "One card, one decision." --quote "don't bundle three questions"
python rules/addrule.py --contract email --like --quote "Could we move Thursday's call to 15:00?" --item r-123 --maps-to H1
python rules/compile_skill.py --contract hub              # print the skill + summary, write nothing
python rules/compile_skill.py --contract hub --install    # write $SODA_SKILLS_DIR/hub/SKILL.md
python rules/ledger_verdict.py classify --surface notif --quote "never show me reactions"
python rules/feedback_session.py --dry                    # classify the open queue, file nothing
python rules/feedback_session.py                          # one pass (put it in cron every 5 minutes)
python rules/rule_stats.py --days 30
```

Config lives in `$SODA_HOME/rule-loop-state.json` (optional): `{"config": {"skill_cap": 15, "retire_after_applicable": 50}}`.
