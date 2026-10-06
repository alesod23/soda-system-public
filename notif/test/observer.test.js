'use strict';
// End-to-end self-test of the notification path with no network and no model: a temp SODA_HOME, a stubbed
// classifier answer, one batch through onFlush, then one collapse tick. Run: node test/observer.test.js
const fs = require('fs');
const os = require('os');
const path = require('path');

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'soda-notif-test-'));
process.env.SODA_HOME = home;
process.env.SODA_HITS = path.join(home, 'rule-hits.jsonl');
process.env.NOTIF_SKILL = path.join(home, 'no-skill.md');
const stub = path.join(home, 'stub.json');
fs.writeFileSync(stub, JSON.stringify({ actionable: [{ from: 'Sam Example', group: '', snippet: 'Can you send the slides by Friday?', urgent: false, when: '10:02', kind: 'dm', suggested_reply: 'Yes, Thursday evening.' }] }));
process.env.SODA_LLM_STUB = stub;

const daemon = require('../daemon');
const collapse = require('../notif-collapse');
const notifLog = require('../notif-log');
const observer = require('../notif-observer');

const fail = (m) => { console.error('FAIL:', m); process.exit(1); };
const now = Date.now();
const iso = (minAgo) => new Date(now - minAgo * 60e3).toISOString();

(async () => {
  const lines = [
    { id: '1', thread: 't1', sender: 'Sam Example', text: 'Can you send the slides by Friday?', ts: iso(10), from_me: false },
    { id: '2', thread: 't2', sender: 'Robin Sample', text: 'Lunch tomorrow?', ts: iso(20), from_me: false },
    { id: '3', thread: 't2', sender: 'me', text: 'Sure, 12:30', ts: iso(15), from_me: true },
    { id: '4', thread: 't3', sender: 'Old News', text: 'This one is three days old', ts: iso(3 * 24 * 60), from_me: false },
  ];
  const batch = lines.filter((l) => !l.from_me);
  const r = await daemon.onFlush('Chat', batch, { lines });
  if (!r || r.items.length !== 1) fail('expected one card with one item');
  if (!r.card.includes('<b>Sam Example</b>')) fail('card does not name the sender');
  const hits = fs.readFileSync(process.env.SODA_HITS, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  const has = (rule, action) => hits.some((h) => h.rule === rule && h.action === action);
  if (!has('H2', 'block')) fail('answered message was not logged as an H2 block');
  if (!has('H4', 'block')) fail('stale message was not logged as an H4 block');
  if (!has('H1', 'ok')) fail('H1 ok missing on the rendered card');

  // collapse: the card is pushed (message id 77), then the owner answers Sam in thread t1
  const e = notifLog.readAll()[0];
  notifLog.setField(e.ts, { tg_message_id: 77 });
  const after = lines.concat([{ id: '5', thread: 't1', sender: 'me', text: 'Sent', ts: new Date(now + 60e3).toISOString(), from_me: true }]);
  const edits = [];
  const out = collapse.tick({ notifLog, hit: observer.hit, esc: (s) => s, now: now + 120e3,
    lines: { Chat: after }, edit: (mid, html) => { edits.push([mid, html]); return true; } });
  if (out.collapsed !== 1 || edits[0][0] !== 77 || !/answered by you/.test(edits[0][1])) fail('card was not collapsed in place');
  if (!notifLog.readAll()[0].collapsed) fail('log entry not marked collapsed');
  const out2 = collapse.tick({ notifLog, hit: observer.hit, now: now + 180e3, lines: { Chat: after }, edit: () => true });
  if (out2.collapsed !== 0) fail('a collapsed card was collapsed twice');

  fs.rmSync(home, { recursive: true, force: true });
  console.log('PASS: answered + stale filtered, card names the sender, hit lines written, answered card collapsed once.');
})().catch((e) => fail(e.stack));
