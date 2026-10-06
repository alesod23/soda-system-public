---
name: hub
description: >
  READ THIS BEFORE POSTING ANY CARD to the approval hub (POST /pending) or acting on a
  verdict. Compiled from HUB-CARD-CONTRACT.md; never edit by hand.
compiled: 2026-10-06
skill_v: 2026-10-06a
ledger: HUB-CARD-CONTRACT.example.md
rules_in_ledger: 7
rules_in_front: 6
---

# Hub (compiled from the ledger, 2026-10-06)

## How to use this

1. Read this before every POST /pending. The hub's guard also applies the patterns in hub-rules.json, but the guard only catches what a regex can see; these rules are the rest.
2. Feedback on a card (a reply, a rejection with a reason, a comment on the review page) goes to the feedback session, which files it in the ledger; this file is its compile (`rules/compile_skill.py --contract hub --install`).
3. In front: what the hit counts say is still needed. Assumed: still checked, back in front the moment it fires.

## The rules in front

### When a card exists

**R1.** Anything that leaves the system toward another person needs a card; the hub sends on yes, a session never sends itself. *e.g.* a reply to a supplier: draft written, card posted with action {type: "email-send-draft", draft_id}, sent by the hub on yes [ledger H1]

**R2.** What the owner already instructed is reported as kind update, never asked again. *e.g.* "done: the calendar slots you asked for are blocked" (update), not "block the calendar slots?" [ledger H2]

**R3.** A card pings once: re-posts carry the same idem key and edit in place; same producer + person + action supersedes. *e.g.* meta {idem: "followup:person-42"} on every revision of the same follow-up [ledger H3]

### What a card shows

**R4.** Line one says decision or update and what exactly; a non-urgent card says when it would become urgent. *e.g.* "Send the revised quote to the supplier? Not urgent before Friday." *liked (H8):* "Send the follow-up to the supplier with the revised quote attached? Draft below, sent from the work account." [ledger H4,H7 +like H8]

**R5.** The artifact is on the card: the whole draft, the recipient, the subject. *e.g.* a draft card with To, Subject and the body in a collapsed quote [ledger H5]

**R6.** One card, one decision; unrelated asks are separate cards. [ledger H6]

## Assumed (still checked by the observer, not in front)


## Procedure

1. Decide first whether a card should exist at all (R1, R2).
2. Give it an idem key so a re-post edits it in place (R3).
3. Line one: decision or update, and what (R4); then the artifact itself (R5).
