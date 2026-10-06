# CRM contract (EXAMPLE ledger, v1)

**Why this file exists.** The CRM owns every follow-up with a person: who they are, what is open between them and the
owner, and the next step with its date. A reader (a model, event-driven) proposes the next step from the person's
state plus what just happened; the owner reviews it. This file is the rule ledger of that surface.

**This is an example.** The rules and quotes below are invented to show the format.

**To add a rule the moment it is given:**
```
python rules/addrule.py --contract crm --rule "..." --quote "the exact words" [--soft] [--item <person id>]
```

## HARD - who owns a person, and what a row must carry

| # | Rule | Source |
|---|---|---|
| H1 | **The CRM owns every follow-up with a person.** A to-do that names a contact is that person's next step in the CRM, never a separate task. *"if it's about a person it lives on the person"* | owner ruling 2026-01-05 |
| H2 | **One person, one next step.** A second proposal replaces the first; it never stacks. *"three open steps for one person means none of them is real"* | owner ruling 2026-01-06 |
| H3 | **Every step carries its origin**: the event or the sentence it came from, linked. *"why is this here? I can't tell where it came from"* | owner ruling 2026-01-07 |

## HARD - what the reader must do

| # | Rule | Source |
|---|---|---|
| H4 | A reply the owner owes comes before anything else on Today. *"people waiting on me first, my own chasing after"* | owner ruling 2026-01-09 |
| H5 | Before proposing a step, check that it did not already happen: a sent message or a calendar event after the step date closes it with the evidence. *"you proposed a follow-up to someone I met yesterday"* | owner ruling 2026-01-13 |
| H6 | Never guess a contact route. No email address or handle is invented from a name pattern; an unknown route is a search, shown for review. *"don't make up emails"* | owner ruling 2026-01-18 |
| H7 | **SOFT, flag only.** Default follow-up cadence is two working days after a message with no reply. *"two days is polite, a week is forgotten"* | owner ruling 2026-01-20 |

## SOFT - judgement

| Rule | Source |
|---|---|
| A CRM row is a proposal of state, not proof; the event history is the proof. | design note |

## Where the code is

- The CRM application itself is not part of this repository. The brain mirrors it (`brain/schema.sql`, schema `crm`)
  so every event can be matched to a person by meaning (`POST /brain/match`).
