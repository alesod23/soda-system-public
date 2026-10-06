'use strict';

// Fast self-test for debounce.js. Uses a 100ms window and simulated arrivals.
// Asserts: a back-to-back burst on one source pools into a SINGLE flush, and
// sources flush independently. Run: node test/debounce.test.js  (exit 0 = pass)

const { Debouncer } = require('../debounce');

const flushes = []; // {source, count, items}
const d = new Debouncer(100, (source, batch) => {
  flushes.push({ source, count: batch.length, items: batch.slice() });
});

function at(ms, fn) {
  setTimeout(fn, ms);
}

// Chat burst: 3 messages, each within the 100ms window of the previous (resets).
at(0, () => d.add('Chat', 'm1'));
at(60, () => d.add('Chat', 'm2'));
at(120, () => d.add('Chat', 'm3'));
// -> Chat silent after 120; flush ~220 with [m1,m2,m3]

// Email: single message at 0 -> flush ~100 with [e1], independent of Chat.
at(0, () => d.add('Email', 'e1'));

// A later, separate Chat message after the first batch already flushed.
at(400, () => d.add('Chat', 'm4'));
// -> flush ~500 with [m4] (a NEW batch, not merged with the first)

at(700, () => {
  const ch = flushes.filter((f) => f.source === 'Chat');
  const em = flushes.filter((f) => f.source === 'Email');

  const fail = (msg) => {
    console.error('FAIL:', msg);
    console.error('flushes:', JSON.stringify(flushes));
    process.exit(1);
  };

  if (em.length !== 1) fail(`Email should flush once, got ${em.length}`);
  if (em[0].count !== 1) fail(`Email batch should be 1, got ${em[0].count}`);

  if (ch.length !== 2) fail(`Chat should flush twice (burst + later), got ${ch.length}`);
  if (ch[0].count !== 3) fail(`Chat first batch should pool 3, got ${ch[0].count}`);
  if (ch[0].items.join(',') !== 'm1,m2,m3') fail(`Chat batch order wrong: ${ch[0].items}`);
  if (ch[1].count !== 1) fail(`Chat second batch should be 1 (m4), got ${ch[1].count}`);

  console.log('PASS: burst pooled into 1, sources independent, new batch separate.');
  console.log('flushes:', JSON.stringify(flushes.map((f) => ({ s: f.source, n: f.count }))));
  process.exit(0);
});
