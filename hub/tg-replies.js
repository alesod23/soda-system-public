'use strict';
/*
 * tg-replies.js: the owner's Telegram replies -> the hub. Optional; run it next to hub/server.js.
 *
 * The private deployment uses a long-lived Claude Code session attached to Telegram for this (it can also answer free
 * text); this script is the minimal, model-free version of the same contract, so the loop works from a phone on day one.
 *
 *   "3 yes" | "3 ok" | "3 send"       -> POST /resolve {id of today's #3, verdict yes}
 *   "3 no" | "3 no: too long"         -> POST /resolve {verdict no, feedback}; the reason goes to the feedback queue
 *   "3 change: shorter, ask first"    -> POST /feedback {surface hub, kind change, item}: a session redoes the artifact
 *   a swipe-reply "yes" / "no: ..."   -> the same, on the card that was replied to
 *   "notif: never show me reactions"  -> POST /feedback {surface notif}, with the quoted notification as the line
 *   "rule: <sentence>"                -> POST /feedback {surface telegram, kind rule}: filed as a rule, whatever it is
 *
 * Only messages from TELEGRAM_CHAT_ID are read. One poller per bot: Telegram allows a single getUpdates consumer, so
 * never run two copies (a second one silently steals the updates from the first).
 */
const E = require('../lib/soda_env');

const TOKEN = E.env('TELEGRAM_BOT_TOKEN', '');
const CHAT = String(E.env('TELEGRAM_CHAT_ID', ''));

async function tg(method, payload) {
  const r = await fetch(`https://api.telegram.org/bot${TOKEN}/${method}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload || {}),
  });
  return r.json();
}
async function hub(method, p, b) {
  const r = await fetch(E.HUB_URL + p, { method, headers: { 'Content-Type': 'application/json' }, body: b ? JSON.stringify(b) : undefined });
  return r.json();
}
const say = (text, reply_to) => tg('sendMessage', { chat_id: CHAT, text, reply_to_message_id: reply_to, disable_notification: true });

// "N" is the day's handle; the newest open card with that seq wins
async function cardBySeq(n) {
  const { items } = await hub('GET', '/pending');
  return items.filter((x) => x.seq === n).pop() || null;
}
async function cardByMessage(mid) {
  const { items } = await hub('GET', '/pending');
  return items.find((x) => x.tg_message_id === mid) || null;
}

async function verdict(card, word, rest, mid) {
  if (/^(yes|y|ok|send)$/i.test(word)) {
    const r = await hub('POST', '/resolve', { id: card.id, verdict: 'yes', feedback: rest || null, device: 'telegram' });
    return say(r.alreadyResolved ? `#${card.seq} was already ${r.verdict}` : `#${card.seq} yes`, mid);
  }
  if (/^no$/i.test(word)) {
    await hub('POST', '/resolve', { id: card.id, verdict: 'no', feedback: rest || null, device: 'telegram' });
    return say(`#${card.seq} no` + (rest ? ' (reason queued for the rule ledger)' : ''), mid);
  }
  if (/^change$/i.test(word) && rest) {
    await hub('POST', '/feedback', { surface: 'hub', kind: 'change', item: card.id, line: card.text, text: rest });
    return say(`#${card.seq} change queued`, mid);
  }
  return null;
}

async function handle(msg) {
  if (String(msg.chat && msg.chat.id) !== CHAT || !msg.text) return;
  const text = msg.text.trim();
  const mid = msg.message_id;
  let m;
  if ((m = /^notif:\s*([\s\S]+)$/i.exec(text))) {
    await hub('POST', '/feedback', { surface: 'notif', item: msg.reply_to_message ? String(msg.reply_to_message.message_id) : null,
      line: msg.reply_to_message ? msg.reply_to_message.text : null, text: m[1] });
    return say('noted for the notification rules', mid);
  }
  if ((m = /^rule:\s*([\s\S]+)$/i.exec(text))) {
    await hub('POST', '/feedback', { surface: 'telegram', kind: 'rule', text: m[1] });
    return say('rule queued', mid);
  }
  if ((m = /^(\d{1,3})\s+(yes|y|ok|send|no|change)\b[:\s]*([\s\S]*)$/i.exec(text))) {
    const card = await cardBySeq(Number(m[1]));
    if (!card) return say(`no open card #${m[1]} today`, mid);
    return verdict(card, m[2], m[3].trim(), mid);
  }
  if (msg.reply_to_message && (m = /^(yes|y|ok|send|no|change)\b[:\s]*([\s\S]*)$/i.exec(text))) {
    const card = await cardByMessage(msg.reply_to_message.message_id);
    if (card) return verdict(card, m[1], m[2].trim(), mid);
  }
  return null;   // anything else is not for the hub
}

async function main() {
  if (!TOKEN || !CHAT) { console.error('TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required'); process.exit(1); }
  let offset = 0;
  console.log('tg-replies: polling (one consumer per bot)');
  for (;;) {
    try {
      const r = await tg('getUpdates', { offset, timeout: 50, allowed_updates: ['message'] });
      for (const u of (r.ok ? r.result : [])) {
        offset = u.update_id + 1;
        if (u.message) await handle(u.message).catch((e) => console.error('handle:', e.message));
      }
      if (!r.ok) await new Promise((res) => setTimeout(res, 5000));
    } catch (e) {
      console.error('poll:', e.message);
      await new Promise((res) => setTimeout(res, 5000));
    }
  }
}
if (require.main === module) main();
module.exports = { handle };
