'use strict';
/*
 * daemon.js: the notification layer. Decides what reaches the owner's phone unasked.
 *
 *   store line (sources/jsonl.js)
 *     -> per-channel debounce (a burst becomes ONE batch)
 *     -> filters, in code: already answered (the owner wrote later in the thread), older than the stale window,
 *        already shown on a card (notif-collapse.filterBatch)
 *     -> classify: one model call per batch. The system prompt = a fixed I/O contract + the COMPILED notif skill,
 *        read from disk on EVERY call, so a rule given five minutes ago is live on the next batch
 *     -> card (who wrote it, group, when, snippet, a suggested reply as a DRAFT, never sent)
 *     -> notification-log.jsonl, observer hit lines (notif-observer.js), push (push.js)
 *   and every NOTIF_COLLAPSE_EVERY_MS the collapse tick folds a card the owner has since answered into one line.
 *
 * Env: NOTIF_STORES, NOTIF_DEBOUNCE_MS (8000), NOTIF_MAXWAIT_MS (22000), NOTIF_PUSH=1 to push
 * (default: log only), SODA_LLM_BIN (claude), SODA_LLM_MODEL (opus), SODA_LLM_STUB=<file> (answer from a file),
 * NOTIF_SKILL (default $SODA_SKILLS_DIR/notif/SKILL.md), NOTIF_PAUSED file to pause.
 * Zero deps.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');
const S = require('../lib/soda_env');
const { Debouncer } = require('./debounce');
const src = require('./sources/jsonl');
const notifLog = require('./notif-log');
const observer = require('./notif-observer');
const collapse = require('./notif-collapse');

const WINDOW_MS = Number(S.env('NOTIF_DEBOUNCE_MS', '8000'));
const MAX_WAIT_MS = Number(S.env('NOTIF_MAXWAIT_MS', '22000'));
const PUSH = path.join(__dirname, 'push.js');
const PAUSED = () => S.env('NOTIF_PAUSED', path.join(S.HOME, 'notif', 'paused'));
const LOG = () => path.join(S.HOME, 'notif', 'daemon.log');
const PUSH_ENABLED = () => S.env('NOTIF_PUSH', '') === '1';
const LLM = () => S.env('SODA_LLM_BIN', 'claude');
const MODEL = () => S.env('SODA_LLM_MODEL', 'opus');
const SKILL = () => S.env('NOTIF_SKILL', path.join(S.SKILLS, 'notif', 'SKILL.md'));
const DOT = '●';

function log(m) {
  const l = `[${new Date().toISOString()}] ${m}`;
  console.log(l);
  try { fs.mkdirSync(path.dirname(LOG()), { recursive: true }); fs.appendFileSync(LOG(), l + '\n'); } catch {}
}

function esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function whenStr(iso) {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return '';
  const tz = S.env('SODA_TZ', 'UTC');
  const hm = d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: tz });
  const day = (x) => x.toLocaleDateString('en-CA', { timeZone: tz });
  return day(d) === day(new Date()) ? hm : `${d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: tz })}, ${hm}`;
}

function readFileSafe(p) { try { return fs.readFileSync(p, 'utf8'); } catch { return ''; } }
function stripFrontmatter(t) { const m = t.match(/^---\r?\n[\s\S]*?\r?\n---\r?\n([\s\S]*)$/); return m ? m[1] : t; }

function buildSystemPrompt(source) {
  let sp =
    `You are a ${source} notification classifier for one person (the owner). You decide which new messages are worth a ` +
    'notification on the owner\'s phone right now.\n' +
    'INPUT (stdin): {channel, messages:[{sender, group, text, when, thread}]} of NEW messages.\n' +
    `OUTPUT: exactly ONE JSON object, no prose: {"actionable":[{"from","group","snippet","urgent":bool,"urgency_reason","when","kind":"group"|"dm","suggested_reply"}]}.\n` +
    'Keep only what needs the owner: a real question, a request, a commitment, a time-sensitive change. Drop reactions, ' +
    'banter, pure closure ("ok", "thanks"), newsletters, receipts and automated mail.\n' +
    'If the same sender sent several messages, combine them into ONE item, texts joined with " / " in order (up to ~300 chars). ' +
    '`from` = the sender verbatim; `group` = the group verbatim or ""; `when` = copy verbatim. `suggested_reply` is a short ' +
    'DRAFT in the owner\'s voice; it is never sent automatically. If nothing is actionable, return {"actionable":[]}.';
  const skill = stripFrontmatter(readFileSafe(SKILL()));
  if (skill) sp += '\n\n=== THE OWNER\'S NOTIFICATION RULES (compiled from the owner\'s ledger; they win over everything above) ===\n' + skill;
  return sp;
}

function parseActionable(raw) {
  let t = String(raw || '').trim();
  const a = t.indexOf('{'); const b = t.lastIndexOf('}');
  if (a >= 0 && b > a) t = t.slice(a, b + 1);
  const r = JSON.parse(t);
  return Array.isArray(r.actionable) ? r.actionable : [];
}

function classify(source, batch) {
  const input = JSON.stringify({ channel: source,
    messages: batch.map((m) => ({ sender: m.sender, group: m.group || '', text: m.text, when: whenStr(m.ts), thread: m.thread })) });
  const stub = S.env('SODA_LLM_STUB', '');
  if (stub) {
    try { return Promise.resolve(parseActionable(fs.readFileSync(stub, 'utf8'))); } catch (e) { log('stub failed: ' + e.message); return Promise.resolve([]); }
  }
  return new Promise((resolve) => {
    const sysTmp = path.join(os.tmpdir(), `notif-sys-${process.pid}-${Date.now()}.txt`);
    try { fs.writeFileSync(sysTmp, buildSystemPrompt(source), 'utf8'); } catch (e) { log('system prompt write failed: ' + e.message); return resolve([]); }
    const args = ['-p', '--strict-mcp-config', '--model', MODEL(), '--system-prompt-file', sysTmp, '--tools', '',
      '--no-session-persistence', '--output-format', 'text'];
    const child = spawn(LLM(), args, { cwd: os.tmpdir(), windowsHide: true });
    let out = ''; let done = false;
    const finish = (v) => { if (done) return; done = true; clearTimeout(timer); try { fs.unlinkSync(sysTmp); } catch {} resolve(v); };
    const timer = setTimeout(() => { try { child.kill('SIGKILL'); } catch {} log('classify TIMEOUT'); finish([]); }, 120000);
    child.stdin.on('error', () => {});
    child.stdin.end(input);
    child.stdout.on('data', (d) => (out += d));
    child.on('error', (e) => { log('classify spawn error: ' + e.message); finish([]); });
    child.on('close', () => { try { finish(parseActionable(out)); } catch { log('classify parse fail'); finish([]); } });
  });
}

function buildCard(source, items) {
  const lines = [`<b>notif</b> · ${esc(source)}`, ''];
  const single = items.length === 1;
  for (const it of items) {
    const lead = single ? '' : '• ';
    const u = it.urgent ? '[!] ' : '';
    const when = it.when ? ` · ${esc(it.when)}` : '';
    const grp = it.group ? ` /${esc(it.group)}` : '';
    lines.push(`${lead}${DOT} ${u}<b>${esc(it.from)}</b>${grp}${when}`);
    let snip = String(it.snippet || '');
    if (snip.length > 300) snip = snip.slice(0, 300) + '...';
    lines.push(`   ${esc(snip)}`);
    if (it.suggested_reply) lines.push(`   draft: <i>${esc(it.suggested_reply)}</i>`);
  }
  return lines.join('\n');
}

function pushCard(card, notifTs) {
  const tmp = path.join(os.tmpdir(), `notif-card-${process.pid}-${Date.now()}.html`);
  try { fs.writeFileSync(tmp, card, 'utf8'); } catch (e) { log('write card failed: ' + e.message); return; }
  const child = spawn(process.execPath, [PUSH, '--file', tmp, '--html'], { windowsHide: true });
  let pout = '';
  child.stdout.on('data', (c) => (pout += c));
  child.on('close', () => {
    const mm = /message_id=(\d+)/.exec(pout);
    if (mm && Number(mm[1]) > 0) notifLog.setField(notifTs, { tg_message_id: Number(mm[1]) });
    try { fs.unlinkSync(tmp); } catch {}
  });
  child.on('error', (e) => log('push spawn error: ' + e.message));
}

async function onFlush(source, batch, opts = {}) {
  if (fs.existsSync(PAUSED())) { log(`flush ${source}: paused, ${batch.length} msg(s) dropped`); return null; }
  const lines = opts.lines || src.storeLines(source);
  const fb = collapse.filterBatch(source, batch, { notifLog, lines });
  const answeredOut = fb.dropped.filter((d) => d.why.startsWith('answered')).map((d) => d.m);
  observer.answeredDrops(source, answeredOut);
  const stale = fb.dropped.filter((d) => d.why.startsWith('older'));
  if (stale.length) observer.hit('H4', 'block', null, source, `${stale.length} msg(s) past the stale window: ` + stale.map((d) => d.m.sender).join('; '));
  const dup = fb.dropped.filter((d) => d.why.startsWith('already on'));
  if (dup.length) observer.hit('dedupe', 'block', null, source, `${dup.length} msg(s) already on a card: ` + dup.map((d) => d.m.sender).join('; '));
  batch = fb.kept;
  if (!batch.length) return null;
  let items = [];
  try { items = await classify(source, batch); } catch (e) { log('classify threw: ' + e.message); }
  if (!items.length) { log(`flush ${source}: ${batch.length} msg(s) -> 0 actionable`); return null; }
  const notifTs = new Date().toISOString();
  const id = 'n' + notifTs.replace(/[^0-9T]/g, '');
  notifLog.append({ ts: notifTs, id, source,
    items: items.map((it) => ({ from: it.from, group: it.group || '', snippet: it.snippet, draft: it.suggested_reply || '' })),
    messages: batch.map((m) => ({ id: m.id, sender: m.sender, group: m.group || '', thread: m.thread, text: m.text, ts: m.ts })) });
  const card = buildCard(source, items);
  observer.observeCard({ source, batch, items, card, id, answered: (m) => src.answered(m, lines) });
  if (PUSH_ENABLED()) { log(`flush ${source}: ${batch.length} msg(s) -> ${items.length} actionable -> push`); pushCard(card, notifTs); }
  else log(`flush ${source}: ${batch.length} -> ${items.length} actionable (log only; NOTIF_PUSH=1 to push):\n${card}`);
  return { id, card, items };
}

function collapseDeps() { return { notifLog, log, hit: observer.hit, push: PUSH, esc }; }

function main() {
  const d = new Debouncer(WINDOW_MS, (source, batch) => { onFlush(source, batch).catch((e) => log('flush failed: ' + e.message)); }, MAX_WAIT_MS);
  const s = src.startJsonlSources((channel, m) => { log(`${channel} in: ${m.sender}: ${String(m.text).slice(0, 50).replace(/\n/g, ' ')}`); d.add(channel, m); });
  const col = collapse.start(collapseDeps);
  log(`notif daemon up (pid ${process.pid}) window=${WINDOW_MS}ms maxwait=${MAX_WAIT_MS}ms collapse every ${col.every / 1000}s; ` +
    `stores: ${s.stores.map((x) => x.channel + '=' + x.file).join(', ')}; push ${PUSH_ENABLED() ? 'ON' : 'off (log only)'}`);
  const stop = () => { d.flushAll(); process.exit(0); };
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
}

module.exports = { classify, buildSystemPrompt, buildCard, onFlush, parseActionable };
if (require.main === module) main();
