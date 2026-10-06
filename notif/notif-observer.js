'use strict';
// notif-observer.js: the observer of the notification surface (docs/01-rule-loop.md, part 5 of a loop).
// One hit line per notif-ledger rule checked on a rendered card, into the shared rule-hits.jsonl. It observes: never
// throws, never blocks, never edits a card. The skill compiler reads the counts.
//   H1  every line of a card names who wrote it (and the group, for a group)
//   H2  never a line the owner already answered (the owner wrote later in that thread)
const fs = require('fs');
const path = require('path');
const S = require('../lib/soda_env');

const HITS = () => S.env('NOTIF_RULE_HITS', S.HITS);
const SKILL = () => S.env('NOTIF_SKILL', path.join(S.SKILLS, 'notif', 'SKILL.md'));
const MODEL = () => S.env('SODA_LLM_MODEL', 'opus');

function skillV() {
  try { const m = /^skill_v:\s*(\S+)/m.exec(fs.readFileSync(SKILL(), 'utf8').slice(0, 1500)); return m ? m[1] : null; }
  catch { return null; }
}

function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function hit(rule, action, item, source, note) {
  const row = { ts: new Date().toISOString(), surface: 'notif', rule, action, item: item || null, model: MODEL(),
    skill_v: skillV(), path: 'notif ' + String(source || '').toLowerCase(), by: 'notif-observer.js',
    note: String(note || '').slice(0, 300) };
  try { fs.mkdirSync(path.dirname(HITS()), { recursive: true }); fs.appendFileSync(HITS(), JSON.stringify(row) + '\n'); }
  catch (e) { try { process.stderr.write('notif hit log failed: ' + e.message + '\n'); } catch {} }
  return row;
}

// H2 before classify: the daemon dropped these messages because the owner wrote later in that thread.
function answeredDrops(source, dropped) {
  try {
    if (!dropped || !dropped.length) return null;
    const who = [...new Set(dropped.map((m) => m.sender || m.thread || '?'))].slice(0, 6).join(', ');
    return hit('H2', 'block', null, source, `${dropped.length} msg(s) not presented, answered later in that thread: ${who}`);
  } catch { return null; }
}

// After render: H1 on every line of the card, H2 again on what reached the card.
function observeCard({ source, batch, items, card, id, answered }) {
  const out = [];
  try {
    const html = String(card || '');
    const nameless = [];
    for (const it of items || []) {
      const from = String(it.from || '').trim();
      const msg = (batch || []).find((b) => b.sender === it.from);
      const groupOk = !(msg && msg.group) || (String(it.group || '').trim() && html.includes('/' + esc(it.group)));
      if (!from || !html.includes('<b>' + esc(from) + '</b>') || !groupOk) {
        nameless.push((from || '(no sender)') + (groupOk ? '' : ' (group missing)'));
      }
    }
    out.push(nameless.length
      ? hit('H1', 'flag', id, source, `${nameless.length} of ${(items || []).length} line(s) without who wrote: ${nameless.join('; ')}`)
      : hit('H1', 'ok', id, source, `${(items || []).length} line(s), each names the sender`));
    if (typeof answered === 'function') {
      const late = [];
      for (const it of items || []) {
        const msg = (batch || []).find((b) => b.sender === it.from);
        let a = false; try { a = !!(msg && answered(msg)); } catch {}
        if (a) late.push(it.from);
      }
      out.push(late.length
        ? hit('H2', 'flag', id, source, `presented although already answered: ${late.join(', ')}`)
        : hit('H2', 'ok', id, source, 'no line already answered'));
    }
  } catch (e) { try { process.stderr.write('notif observer failed: ' + e.message + '\n'); } catch {} }
  return out;
}

module.exports = { observeCard, answeredDrops, hit, HITS };
