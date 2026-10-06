'use strict';
// soda_env.js: the Node twin of lib/soda_paths.py. Reads <repo>/.env (existing environment variables win) and
// says where the data lives. Zero dependencies.
const fs = require('fs');
const path = require('path');

const REPO = path.resolve(__dirname, '..');

function loadDotenv(file) {
  const p = file || path.join(REPO, '.env');
  let text = '';
  try { text = fs.readFileSync(p, 'utf8'); } catch { return; }
  for (let line of text.split(/\r?\n/)) {
    line = line.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    if (line.startsWith('export ')) line = line.slice(7);
    const i = line.indexOf('=');
    const k = line.slice(0, i).trim();
    let v = line.slice(i + 1).trim();
    if (v.length >= 2 && v[0] === v[v.length - 1] && (v[0] === '"' || v[0] === "'")) v = v.slice(1, -1);
    if (process.env[k] === undefined) process.env[k] = v;
  }
}
loadDotenv();

const expand = (p) => (p && p.startsWith('~') ? path.join(process.env.HOME || '', p.slice(1)) : p);
const env = (k, d) => (process.env[k] != null && process.env[k] !== '' ? expand(process.env[k]) : d);

const HOME = env('SODA_HOME', path.join(REPO, 'data'));
module.exports = {
  REPO,
  env,
  HOME,
  HITS: env('SODA_HITS', path.join(HOME, 'rule-hits.jsonl')),
  DECISIONS: env('SODA_DECISIONS', path.join(HOME, 'decisions.jsonl')),
  FEEDBACK_QUEUE: env('SODA_FEEDBACK_QUEUE', path.join(HOME, 'feedback', 'queue.jsonl')),
  SKILLS: env('SODA_SKILLS_DIR', path.join(HOME, 'skills')),
  HUB_URL: (env('HUB_URL', 'http://127.0.0.1:4180')).replace(/\/+$/, ''),
  appendJsonl(file, obj) {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.appendFileSync(file, JSON.stringify(obj) + '\n', 'utf8');
  },
};
