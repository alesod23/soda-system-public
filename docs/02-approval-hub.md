# 2. The approval hub

One rule holds the whole system together: **nothing reaches another person without the owner's yes, and the owner
says yes from the phone.** Sessions draft; the hub asks; the hub sends.

```
 producers                          hub/server.js (:4180)                               the owner
 ---------                          ---------------------                               ---------
 drafting session  --POST /pending-->  guard (hub-rules.json) --> card #N --> Telegram --> "3 yes" / "3 no: too long"
 notif layer                           idem: same subject = same card, edited in place        |      (hub/tg-replies.js)
 system agent                          supersede: same producer+person+action                 |      or the review page GET /
 feedback session                           |                                                  v
                                            | decisions.jsonl  <------------------------ POST /resolve
                                            | rule-hits.jsonl (every guard decision)          |
                                            v                                                  |
                                     yes: executors.json[action.type] runs (argv, no shell) <--+
                                     no + reason / any comment: POST /feedback --> feedback queue --> rule loop
```

## A card

```json
POST /pending
{
  "text": "Send the revised quote to the supplier?",
  "context": "They asked on Monday; the quote is attached to the draft.",
  "kind": "decision",
  "type": "email-draft",
  "meta": {"origin": "drafting", "person": "person-42", "to": "supplier@example.com",
           "subject": "Revised quote", "body": "Hi ...", "idem": "quote:person-42"},
  "action": {"type": "email-send-draft", "draft_id": "r-123", "account": "work"}
}
-> {"ok": true, "id": "a1b2c3", "seq": 3, "guard": "ok"}
```

- `kind`: `decision` (yes/no) or `update` (told, nothing to approve; no buttons).
- `type`: `email-draft`, `chat-draft`, `social-draft` render the draft itself in a collapsed quote; anything else
  renders `text` + `context`.
- `action`: what a yes runs. The hub looks up `action.type` in `$SODA_HOME/hub/executors.json`, fills `{field}`
  placeholders from the action and runs the argv **without a shell**. The result (exit code, tail of output) is stored
  on the card and reported in one Telegram line. The hub never composes a message: it only releases one the owner has
  already read on the card.
- `silent: true` registers the card (same numbering) but pushes nothing, for a loop that lives in another channel.

## Why each mechanism exists

| Mechanism | The failure it came from |
|---|---|
| **Daily #N** | "proposal 11" must mean the same card on the phone, the page and in a reply. The number resets every local day (`SODA_TZ`). |
| **The guard** | A session asked to confirm a change the owner had already ordered in another session. A rule in a memory file did not stop it, because that session never read it. The check now lives in the one code path every card passes, and its patterns are filed from the owner's own sentences (`addrule.py --contract hub --suppress`). `downgrade` turns the question into an update; `drop` keeps the card silent. |
| **idem** | The same subject was re-posted as a new card after every revision, and the phone buzzed each time. Now a re-post with the same key edits the card in place, silently, and a resolved card is reopened with a fresh #N. |
| **supersede** | A producer re-ran and posted a second card about the same person and action. Now the newer one closes its open twin (`meta.origin` + `meta.person` + action). |
| **/close never runs the action** | When the owner already did the thing by hand (the email shows up in Sent), the card must close without a second send, and without recording a "yes" the owner never gave. |
| **decisions.jsonl** | State is pruned; the verdicts are the dataset. Every verdict is one durable line. |
| **A "no" is feedback** | A rejection with a reason used to be read once and lost. It is now a queue entry the feedback session files as a rule ([01-rule-loop.md](01-rule-loop.md)). |

## Telegram

The hub pushes with the Bot API (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). Without a token it prints each card to its
log and keeps working, which is how the tests run.

Replies come back through `hub/tg-replies.js` (optional): `3 yes`, `3 no: why`, `3 change: what`, a swipe-reply on a
card, `notif: ...` and `rule: ...`. Only the configured chat is read. **Run exactly one poller per bot**: Telegram
allows a single `getUpdates` consumer, and a second one silently steals updates from the first. (The private
deployment uses a long-running Claude Code session attached to Telegram as that consumer, which also handles free
text; the script is the model-free equivalent.)

## The review page

`GET /` serves `hub/review.html`: open cards with Yes / No and a "why" box, the last day's verdicts with their action
results, and the feedback queue with what each sentence became. Bind the hub to localhost (the default) and reach it
through an authenticated tunnel or VPN if you need it from elsewhere; it has no login of its own.

## Routes

| Route | What |
|---|---|
| `POST /pending` | create (or, with `idem`, update in place) a card |
| `POST /resolve {id, verdict, feedback?, device?}` | first device wins; yes runs the action; a reason is queued as feedback |
| `POST /revise {id, text?, context?, meta?, action?, note?}` | same card, same #N; the Telegram message is edited, then one "revised" line |
| `POST /close {id, reason, verdict?}` | resolve without running anything |
| `GET /pending`, `GET /recent`, `GET /item/<id>` | read |
| `POST /feedback {surface, item?, line?, text, kind?}` | queue one sentence for the feedback session |
| `GET /feedback?status=open`, `POST /feedback/done {ids, results}` | the feedback session's side of the queue |
