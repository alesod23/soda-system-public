---
name: crm
description: >
  READ THIS BEFORE WRITING TO THE CRM (a person, a next step, a note) and, for the
  reader, BEFORE DECIDING A PERSON'S NEXT STEP. Compiled from CRM-CONTRACT.md; never
  edit by hand.
compiled: 2026-10-06
skill_v: 2026-10-06a
ledger: CRM-CONTRACT.example.md
rules_in_ledger: 7
rules_in_front: 5
---

# CRM (compiled from the ledger, 2026-10-06)

## How to use this

1. The reader proposes one next step per person from the person's state plus the new event; this skill is in its prompt on every call.
2. In front: what the hit counts say is still needed. Assumed: still checked, back in front the moment it fires.

## The rules in front

### Who owns a person

**R1.** One person, one next step: a new proposal replaces the old one. *e.g.* "follow up Tuesday" replaces "follow up Friday", never both [ledger H2]

**R2.** Every step carries its origin: the event or the sentence it came from. *e.g.* step "send the deck" with origin "meeting note 2026-01-07, their ask at the end" [ledger H3]

### What the reader does

**R3.** A reply the owner owes comes before anything else on Today. [ledger H4]

**R4.** Before proposing a step, check it did not already happen (sent message or calendar event after the step date closes it); default cadence two working days after an unanswered message. *e.g.* a follow-up proposed for someone met yesterday: closed with the calendar event as evidence [ledger H5,H7]

**R5.** Never guess a contact route; an unknown route is a search, shown for review. *e.g.* no first.last@company guess: "route unknown, search proposed" [ledger H6]

## Assumed (still checked by the observer, not in front)

- A to-do that names a contact is that person's next step in the CRM, never a separate task. [ledger H1]

## Procedure

1. Check the step did not already happen (R4).
2. Owed replies first (R3); one step per person (R1) with its origin (R2).
