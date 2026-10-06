'use strict';
/*
 * approval-hub: the single place where the system asks its owner for a yes.
 *
 * Every producer (a drafting session, the notification layer, the system agent, a cron job) that wants to do something
 * only the owner may allow (send a message to a person, spend money, change a rule) POSTs a CARD here. The card reaches
 * the owner's phone through Telegram, numbered #N for the day; the owner answers "N yes" / "N no: <why>" (see
 * tg-replies.js) or on the review page (GET /). On yes the hub itself runs the card's action through a whitelisted
 * executor: the session that wrote the draft never sends anything.
 *
 * What makes it more than a queue (each one came from a real failure, see docs/02-approval-hub.md):
 *   - THE GUARD. Before a card reaches the phone it is matched against hub-rules.json, patterns filed from the owner's
 *     own sentences (`rules/addrule.py --contract hub --suppress <regex>`): "downgrade" turns a question into an
 *     update (told, not asked), "drop" keeps the card but pushes nothing. One sentence = a written rule + a live filter.
 *   - ONE PING PER SUBJECT. A re-post with the same `idem` key edits the existing card in place, silently. A new card
 *     from the same producer about the same person and action supersedes the open one.
 *   - DURABLE VERDICTS. Every verdict is one line in decisions.jsonl; every guard decision is one hit line in
 *     rule-hits.jsonl (the rule loop's measurement).
 *   - A "no" IS FEEDBACK. A rejection with a reason, or any comment on a card, goes to the feedback queue, where the
 *     feedback session turns it into a ledger row (rules/feedback_session.py). The hub is the queue's only writer.
 *
 * Zero dependencies (Node 18+ for fetch). State in $SODA_HOME/hub/state.json, written atomically.
 *
 *   POST /pending  {text, context?, kind?: "decision"|"update", type?, action?, meta?, idem?, silent?}  -> {ok, id, seq}
 *   POST /resolve  {id, verdict: "yes"|"no", feedback?, device?}  runs the action on yes
 *   POST /revise   {id, text?, context?, meta?, action?, note?}   same card, same #N, Telegram message edited in place
 *   POST /close    {id, reason, verdict?}                          resolves WITHOUT running the action
 *   GET  /pending  open cards;   GET /item/<id>  one card
 *   POST /feedback {surface, item?, line?, text, kind?}            one sentence for the feedback session
 *   GET  /feedback?status=open                                     the queue;   POST /feedback/done {ids, result}
 *   GET  /         the review page
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const E = require('../lib/soda_env');

const PORT = Number(E.env('HUB_PORT', '4180'));
const HOST = E.env('HUB_HOST', '127.0.0.1');
const DIR = path.join(E.HOME, 'hub');
const STATE = path.join(DIR, 'state.json');
const HUB_RULES = E.env('HUB_RULES', path.join(DIR, 'hub-rules.json'));
const EXECUTORS = E.env('HUB_EXECUTORS', path.join(DIR, 'executors.json'));
const TG_TOKEN = E.env('TELEGRAM_BOT_TOKEN', '');
const TG_CHAT = E.env('TELEGRAM_CHAT_ID', '');
const TZ = E.env('SODA_TZ', 'UTC');
const TG_MAX = 3500;            // Telegram's hard limit is 4096 characters per message
const KEEP_RESOLVED_DAYS = 7;   // decisions.jsonl is the durable record; state.json only holds what is live

// ------------------------------------------------------------------ state

function load() {
  try { return JSON.parse(fs.readFileSync(STATE, 'utf8')); } catch { return { pending: {}, idem_cards: {} }; }
}
function save(s) {
  fs.mkdirSync(DIR, { recursive: true });
  const tmp = STATE + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(s, null, 2));
  fs.renameSync(tmp, STATE);
}
function today() { return new Date().toLocaleDateString('sv-SE', { timeZone: TZ }); }
function nextSeq(s) {   // "#N" resets every local day: the phone, the page and the owner's "3 yes" share one handle
  if (!s.seq || s.seq.date !== today()) s.seq = { date: today(), n: 0 };
  s.seq.n += 1;
  return s.seq.n;
}
function newId() { return 'a' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
function prune(s) {
  const cut = Date.now() - KEEP_RESOLVED_DAYS * 864e5;
  for (const [id, it] of Object.entries(s.pending)) {
    if (!it.resolved || new Date(it.resolved_at || it.created_at).getTime() >= cut) continue;
    if (it.idem) { s.idem_cards = s.idem_cards || {}; s.idem_cards[it.idem] = it; }   // a later re-post reopens it
    delete s.pending[id];
  }
}

// ------------------------------------------------------------------ ledgers (append-only jsonl)

function logDecision(item, verdict, by, feedback) {
  try {
    E.appendJsonl(E.DECISIONS, {
      ts: new Date().toISOString(), surface: 'hub', seq: item.seq, item: item.id, verdict: verdict || null,
      by: by || null, type: item.type || null, kind: item.kind || null, guard: (item.guard && item.guard.rule) || null,
      created_at: item.created_at || null, feedback: feedback ? String(feedback).slice(0, 300) : null,
    });
  } catch {}
}
function logHit(rec) {
  try {
    E.appendJsonl(E.HITS, Object.assign({ ts: new Date().toISOString(), surface: 'hub', rule: null, action: 'ok',
      item: null, model: null, skill_v: null, path: null, by: 'hub-guard', note: '' }, rec));
  } catch {}   // the ledger never breaks a card
}

// ------------------------------------------------------------------ the guard

function loadRules() {
  try {
    const r = JSON.parse(fs.readFileSync(HUB_RULES, 'utf8'));
    return { downgrade: Array.isArray(r.downgrade) ? r.downgrade : [], drop: Array.isArray(r.drop) ? r.drop : [] };
  } catch { return { downgrade: [], drop: [] }; }   // no rules file = no filtering, never a crash
}
// -> {action: 'ok'|'downgrade'|'drop', rule, note}. Matches text + context: the tell is often in the context.
function evalRules(item) {
  const hay = (item.text || '') + '\n' + (item.context || '');
  const rules = loadRules();
  for (const [bucket, list] of [['drop', rules.drop], ['downgrade', rules.downgrade]]) {
    for (const r of list) {
      try { if (new RegExp(r.match, 'i').test(hay)) return { action: bucket, rule: r.rule || null, note: r.note || '' }; } catch {}
    }
  }
  return { action: 'ok', rule: null, note: '' };
}

// producer + person + action of a card, from its meta; null when any part is missing (then nothing is superseded)
function superKey(b) {
  const m = (b && b.meta) || {};
  const producer = m.origin || null;
  const person = m.person || null;
  const action = m.action || (b.action && b.action.type) || b.type || null;
  if (!producer || !person || !action) return null;
  return [producer, person, action].map((x) => String(x).toLowerCase()).join('|');
}
function idemKey(b) {
  const k = (b && b.idem) || (b && b.meta && b.meta.idem) || null;
  return k ? String(k).slice(0, 300) : null;
}

// ------------------------------------------------------------------ Telegram (Bot API, HTML parse mode)

function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

async function tg(method, payload) {
  if (!TG_TOKEN || !TG_CHAT) {   // no phone configured: the card is printed, the hub keeps working
    if (method !== 'editMessageText') console.log(`[telegram:${method} (dry, no token)]\n${payload.text}\n`);
    return null;
  }
  try {
    const r = await fetch(`https://api.telegram.org/bot${TG_TOKEN}/${method}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(Object.assign({ chat_id: TG_CHAT, parse_mode: 'HTML', disable_web_page_preview: true }, payload)),
    });
    const j = await r.json();
    if (!j.ok) console.error(`telegram ${method} failed: ${j.description || r.status}`);
    return j.ok ? j.result : null;
  } catch (e) { console.error(`telegram ${method} error: ${e.message}`); return null; }
}

const DRAFT_TYPES = new Set(['email-draft', 'chat-draft', 'social-draft']);

function renderCard(it) {
  const m = it.meta || {};
  const isUpdate = String(it.kind || '').toLowerCase() === 'update';
  let html = '<b>Approval hub</b>\n';
  if (DRAFT_TYPES.has(it.type)) {
    html += '<b>' + esc(it.text) + '</b>';
    if (m.subject) html += '\n<i>' + esc(m.subject) + '</i>';
    if (m.to) html += '\nTo: ' + esc(m.to);
    let body = String(m.body || '').trim();
    if (body.length > 3000) body = body.slice(0, 3000) + '\n[...]';
    if (body) html += '\n<blockquote expandable>' + esc(body) + '</blockquote>';   // the artifact itself, collapsed
    if (m.critic_line) html += '\n' + esc(m.critic_line);
  } else {
    html += (isUpdate ? 'update: ' : 'approve? ') + '<b>' + esc(it.text) + '</b>';
    let ctx = String(it.context || '').trim();
    if (ctx.length > TG_MAX) ctx = ctx.slice(0, TG_MAX) + '\n[...] (full text on the review page)';
    if (ctx) html += '\n\n' + esc(ctx);
  }
  if (it.guard && it.guard.action === 'downgrade') html += '\n<i>(posted as a question; the guard made it an update: ' + esc(it.guard.rule || '') + ')</i>';
  if (!isUpdate) html += `\n\nReply <b>${it.seq} yes</b> · <b>${it.seq} no: why</b> · <b>${it.seq} change: what</b>`;
  return html;
}

async function pushCard(id) {
  const s = load(); const it = s.pending[id];
  if (!it || it.silent || (it.guard && it.guard.action === 'drop')) return;
  const res = await tg('sendMessage', { text: renderCard(it) });
  if (res && res.message_id) { const s2 = load(); if (s2.pending[id]) { s2.pending[id].tg_message_id = res.message_id; save(s2); } }
}
async function editCard(it) {
  if (it.silent || !it.tg_message_id) return;
  await tg('editMessageText', { message_id: it.tg_message_id, text: renderCard(it) });   // edits never buzz the phone
}
function pushLine(html) { tg('sendMessage', { text: html, disable_notification: true }); }

// ------------------------------------------------------------------ executors: what a yes runs

// executors.json: {"<action.type>": {"argv": ["python3", "send.py", "--draft-id", "{draft_id}"], "timeout_s": 120}}.
// Placeholders are filled from the card's action object; the command runs WITHOUT a shell, so a value can never become
// a second command. An action type with no executor is simply recorded: the yes is still durable in decisions.jsonl.
function loadExecutors() { try { return JSON.parse(fs.readFileSync(EXECUTORS, 'utf8')); } catch { return {}; } }

function executeAction(id) {
  const s = load(); const it = s.pending[id];
  const a = it && it.action;
  if (!a || !a.type) return;
  const ex = loadExecutors()[a.type];
  const record = (res) => {
    const s2 = load(); if (!s2.pending[id]) return;
    s2.pending[id].action_result = Object.assign({ at: new Date().toISOString() }, res); save(s2);
    pushLine((res.ok ? 'done' : 'FAILED') + ` <b>#${it.seq}</b> ${esc(a.type)}` + (res.ok ? '' : ` (exit ${res.code})\n<code>${esc(res.tail || '')}</code>`));
  };
  if (!ex || !Array.isArray(ex.argv)) { record({ ok: false, code: null, tail: `no executor for action type "${a.type}" in executors.json` }); return; }
  const argv = ex.argv.map((x) => String(x).replace(/\{(\w+)\}/g, (_, k) => (a[k] == null ? '' : String(a[k]))));
  let out = '';
  const p = spawn(argv[0], argv.slice(1), { cwd: E.REPO, windowsHide: true });
  const timer = setTimeout(() => { try { p.kill('SIGKILL'); } catch {} }, (ex.timeout_s || 120) * 1000);
  p.stdout.on('data', (d) => { out += d; });
  p.stderr.on('data', (d) => { out += d; });
  p.on('close', (code) => { clearTimeout(timer); record({ ok: code === 0, code, tail: out.trim().slice(-600) }); });
  p.on('error', (e) => { clearTimeout(timer); record({ ok: false, code: null, tail: e.message }); });
}

// ------------------------------------------------------------------ the feedback queue (read by rules/feedback_session.py)

function readQueue() {
  try {
    return fs.readFileSync(E.FEEDBACK_QUEUE, 'utf8').split('\n').filter(Boolean)
      .map((l) => { try { return JSON.parse(l); } catch { return null; } }).filter(Boolean);
  } catch { return []; }
}
function writeQueue(entries) {
  fs.mkdirSync(path.dirname(E.FEEDBACK_QUEUE), { recursive: true });
  const tmp = E.FEEDBACK_QUEUE + '.tmp';
  fs.writeFileSync(tmp, entries.map((e) => JSON.stringify(e)).join('\n') + (entries.length ? '\n' : ''));
  fs.renameSync(tmp, E.FEEDBACK_QUEUE);
}
function queueFeedback(f) {
  const text = String(f.text || '').trim();
  if (!text) return null;
  const e = { id: 'f' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), ts: new Date().toISOString(),
    surface: String(f.surface || 'hub'), kind: String(f.kind || 'feedback'), item: f.item ? String(f.item) : null,
    line: f.line ? String(f.line).slice(0, 1500) : null, text: text.slice(0, 2000), status: 'open' };
  E.appendJsonl(E.FEEDBACK_QUEUE, e);
  return e;
}

// ------------------------------------------------------------------ HTTP

function body(req) {
  return new Promise((resolve) => {
    let d = '';
    req.on('data', (c) => { d += c; if (d.length > 1e6) req.destroy(); });
    req.on('end', () => { try { resolve(d ? JSON.parse(d) : {}); } catch { resolve({}); } });
  });
}
function json(res, code, obj) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(obj));
}
const PAGE = fs.readFileSync(path.join(__dirname, 'review.html'), 'utf8');

const server = http.createServer(async (req, res) => {
  const u = new URL(req.url, 'http://x');
  const s = load();

  if (req.method === 'GET' && u.pathname === '/') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' }); return res.end(PAGE);
  }
  if (req.method === 'GET' && u.pathname === '/pending') {
    const items = Object.values(s.pending).filter((x) => !x.resolved).sort((a, b) => a.created_at.localeCompare(b.created_at));
    return json(res, 200, { items });
  }
  if (req.method === 'GET' && u.pathname === '/recent') {   // the review page: open first, then the last day's verdicts
    const cut = Date.now() - 864e5;
    const items = Object.values(s.pending).filter((x) => !x.resolved || new Date(x.resolved_at).getTime() > cut)
      .sort((a, b) => (a.resolved - b.resolved) || b.created_at.localeCompare(a.created_at));
    return json(res, 200, { items });
  }
  if (req.method === 'GET' && u.pathname.startsWith('/item/')) {
    const it = s.pending[decodeURIComponent(u.pathname.slice(6))];
    return it ? json(res, 200, it) : json(res, 404, { error: 'not found' });
  }

  if (req.method === 'POST' && u.pathname === '/pending') {
    const b = await body(req);
    if (!b.text) return json(res, 400, { ok: false, error: 'text required' });
    const now = new Date().toISOString();

    // ONE PING PER SUBJECT: same idem key = the same card, updated in place (reopened with a fresh #N if resolved)
    const ikey = idemKey(b);
    if (ikey) {
      let it = Object.values(s.pending).find((x) => x.idem === ikey && !x.resolved)
        || Object.values(s.pending).filter((x) => x.idem === ikey).sort((x, y) => y.created_at.localeCompare(x.created_at))[0]
        || (s.idem_cards && s.idem_cards[ikey]) || null;
      if (it) {
        if (s.idem_cards) delete s.idem_cards[ikey];
        s.pending[it.id] = it;
        const reopened = !!it.resolved;
        if (reopened) {
          it.history = (it.history || []).concat([{ verdict: it.verdict, resolved_by: it.resolved_by, resolved_at: it.resolved_at }]).slice(-20);
          Object.assign(it, { resolved: false, verdict: null, resolved_by: null, resolved_at: null, seq: nextSeq(s) });
        }
        for (const k of ['text', 'context', 'action', 'kind', 'type']) if (b[k] !== undefined) it[k] = b[k];
        if (b.meta && typeof b.meta === 'object') it.meta = Object.assign({}, it.meta || {}, b.meta);
        it.updated_at = now; it.updates = (it.updates || 0) + 1;
        save(s);
        logHit({ rule: 'idem', action: reopened ? 'reopen' : 'update', item: it.id, path: ikey, note: 'in place, no new push' });
        editCard(it);
        return json(res, 200, { ok: true, id: it.id, seq: it.seq, updated: true, reopened, pushed: false });
      }
    }

    const id = b.id || newId();
    // SUPERSEDE: a new card from the same producer about the same person and action replaces its open twin
    const skey = superKey(b);
    if (skey) {
      for (const old of Object.values(s.pending)) {
        if (old.resolved || old.skey !== skey) continue;
        Object.assign(old, { resolved: true, verdict: null, resolved_by: 'close:superseded by ' + id, resolved_at: now });
        logDecision(old, null, old.resolved_by, null);
        logHit({ rule: 'supersede', action: 'close', item: old.id, path: skey, note: 'replaced by ' + id });
      }
    }
    const it = s.pending[id] = {
      id, seq: nextSeq(s), skey, idem: ikey, text: String(b.text), context: b.context || null, kind: b.kind || 'decision',
      type: b.type || null, action: b.action || null, meta: b.meta || null, silent: !!b.silent, tg_message_id: null,
      created_at: now, resolved: false, verdict: null, resolved_by: null, resolved_at: null,
    };
    // THE GUARD runs before anything reaches the owner
    const g = evalRules(it);
    if (g.action !== 'ok') it.guard = g;
    if (g.action === 'downgrade') it.kind = 'update';   // told, not asked
    prune(s); save(s);
    logHit({ rule: g.rule, action: g.action, item: id, path: (b.meta && b.meta.origin) || null, note: g.note });
    pushCard(id);
    return json(res, 200, { ok: true, id, seq: it.seq, guard: g.action });
  }

  if (req.method === 'POST' && u.pathname === '/resolve') {
    const b = await body(req);
    const it = s.pending[b.id];
    if (!it) return json(res, 404, { ok: false, error: 'unknown id' });
    if (it.resolved) return json(res, 200, { ok: true, alreadyResolved: true, verdict: it.verdict });   // first device wins
    const fb = b.feedback && String(b.feedback).trim() ? String(b.feedback).trim() : null;
    Object.assign(it, { resolved: true, verdict: b.verdict === 'yes' ? 'yes' : 'no', resolved_by: String(b.device || 'unknown'),
      resolved_at: new Date().toISOString(), feedback: fb });
    save(s);
    logDecision(it, it.verdict, it.resolved_by, fb);
    if (it.verdict === 'yes') executeAction(it.id);
    // A reason given with a verdict is a sentence of feedback on this card: the feedback session files it.
    if (fb) queueFeedback({ surface: 'hub', kind: 'card-verdict', item: it.id, line: it.text + (it.context ? '\n' + it.context : ''), text: fb });
    if (it.verdict === 'no' && !fb) pushLine(`<b>#${it.seq}</b> rejected. Reply <b>${it.seq} change: what</b> and it is redone.`);
    return json(res, 200, { ok: true, verdict: it.verdict, feedback: fb });
  }

  if (req.method === 'POST' && u.pathname === '/revise') {   // same card, same #N for its whole life
    const b = await body(req);
    const it = s.pending[b.id];
    if (!it) return json(res, 404, { ok: false, error: 'unknown id' });
    if (it.resolved) return json(res, 409, { ok: false, error: 'already resolved', verdict: it.verdict });
    for (const k of ['text', 'context', 'action']) if (b[k] !== undefined) it[k] = b[k];
    if (b.meta && typeof b.meta === 'object') it.meta = Object.assign({}, it.meta || {}, b.meta);
    it.revised_at = new Date().toISOString(); it.revisions = (it.revisions || 0) + 1;
    save(s);
    await editCard(it);
    if (!it.silent) pushLine(`<b>#${it.seq} revised</b>` + (b.note ? ' · ' + esc(b.note) : '') + ' · the card above is updated');
    return json(res, 200, { ok: true, id: it.id, seq: it.seq, revisions: it.revisions });
  }

  if (req.method === 'POST' && u.pathname === '/close') {   // e.g. the owner already did it by hand: never run the action
    const b = await body(req);
    const it = s.pending[b.id];
    if (!it) return json(res, 404, { ok: false, error: 'unknown id' });
    if (it.resolved) return json(res, 200, { ok: true, alreadyResolved: true, verdict: it.verdict });
    // a close is not the owner's tap: it carries a verdict only when the caller states one, never a default yes
    Object.assign(it, { resolved: true, verdict: b.verdict === 'yes' || b.verdict === 'no' ? b.verdict : null,
      resolved_by: 'close:' + String(b.reason || 'unspecified'), resolved_at: new Date().toISOString() });
    save(s);
    logDecision(it, it.verdict, it.resolved_by, null);
    return json(res, 200, { ok: true, verdict: it.verdict });
  }

  if (req.method === 'POST' && u.pathname === '/feedback') {
    const b = await body(req);
    const e = queueFeedback(b);
    return e ? json(res, 200, { ok: true, id: e.id }) : json(res, 400, { ok: false, error: 'text required' });
  }
  if (req.method === 'GET' && u.pathname === '/feedback') {
    const st = u.searchParams.get('status');
    return json(res, 200, { entries: readQueue().filter((e) => !st || e.status === st) });
  }
  if (req.method === 'POST' && u.pathname === '/feedback/done') {
    const b = await body(req);
    const ids = new Set(Array.isArray(b.ids) ? b.ids.map(String) : []);
    let n = 0;
    const q = readQueue().map((e) => {
      if (!ids.has(e.id) || e.status !== 'open') return e;
      n += 1;
      const r = (b.results && b.results[e.id]) || b.result || null;
      return Object.assign(e, { status: 'done', done_at: new Date().toISOString(), result: r });
    });
    writeQueue(q);
    return json(res, 200, { ok: true, marked: n });
  }

  json(res, 404, { error: 'no route' });
});

if (require.main === module) {
  server.listen(PORT, HOST, () => console.log(`approval-hub on http://${HOST}:${PORT}` + (TG_TOKEN ? '' : ' (no Telegram token: cards are printed here)')));
}
module.exports = { server, renderCard, evalRules, superKey };
