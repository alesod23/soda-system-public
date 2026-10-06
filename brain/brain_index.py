#!/usr/bin/env python3
"""SODA BRAIN indexer: markdown pages -> brain.pages / brain.chunks (pgvector).

Sources (kind=glob), default:
    memory=<BRAIN_REPO>/memory/*.md   system=<BRAIN_REPO>/system/*.md   handoff=<BRAIN_REPO>/handoffs/*.md
    rule=<SODA_HOME>/ledgers/*-CONTRACT.md   (the rule ledgers, one page per rule row)
Override with BRAIN_SOURCES="kind=glob,kind=glob"; the markdown root with BRAIN_REPO (default: <repo>/examples/brain).

A contract file yields one page per rule row (`| H1 | **rule** | source |`) plus one page for its prose.
Chunks: ~400 estimated tokens (~180 words), 50 overlap, split by heading first (fits the model's 512). Embeddings: intfloat/multilingual-e5-small
("passage: " / "query: " prefixes), batches of 64, CPU. Fast paths: a page whose sha is unchanged is
skipped entirely; a chunk whose (title, text) is unchanged keeps its stored embedding.

    brain_index.py              index (needs SODA_DB_DSN, from the environment or the repo's .env)
    brain_index.py --dry-run    print the plan without a DB
    brain_index.py --stats      print counts from the DB
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths  # noqa: E402  (reads <repo>/.env; existing environment variables win)

MODEL_NAME = os.environ.get("BRAIN_MODEL", "intfloat/multilingual-e5-small")
# Chunk budget in ESTIMATED model tokens (1 + len(word)//4 per word, calibrated on the memory corpus:
# est/real = 0.94). multilingual-e5-small sees 512 tokens; 400 + the "passage: <title>" prefix fits
# (measured on the memory corpus: median 390 real tokens, p90 480). With 600 words (the first draft)
# the model cut 68% of the chunks short; with 440 still 13%.
CHUNK_TOKENS = int(os.environ.get("BRAIN_CHUNK_TOKENS", "400"))
CHUNK_OVERLAP = int(os.environ.get("BRAIN_CHUNK_OVERLAP", "50"))
EMBED_BATCH = 64
KINDS = ("memory", "system", "handoff", "rule", "hint", "other")

_HEADING_RE = re.compile(r"^#{1,6}\s")
_RULE_ROW_RE = re.compile(r"^\|\s*([A-Z]\d+)\s*\|(.*)\|\s*(.*?)\s*\|\s*$")
_TOKEN_RE = re.compile(r"\S+|\n+")


# ----------------------------------------------------------------- env

def load_env(path: str | os.PathLike | None = None) -> None:
    """Read KEY=VALUE lines from an env file (default: <repo>/.env) into os.environ; existing vars win."""
    soda_paths.load_dotenv(Path(path) if path else None)


def repo_root() -> Path:
    """The markdown root that holds memory/, system/, handoffs/ (env BRAIN_REPO, default <repo>/examples/brain)."""
    v = os.environ.get("BRAIN_REPO")
    return Path(v).expanduser() if v else soda_paths.REPO / "examples" / "brain"


def default_sources(repo: Path | None = None) -> list[tuple[str, str]]:
    repo = repo or repo_root()
    return [("memory", str(repo / "memory" / "*.md")),
            ("system", str(repo / "system" / "*.md")),
            ("handoff", str(repo / "handoffs" / "*.md")),
            ("rule", str(soda_paths.LEDGERS / "*-CONTRACT.md"))]


def parse_sources(spec: str | None, repo: Path | None = None) -> list[tuple[str, str]]:
    if not spec:
        return default_sources(repo)
    out = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        kind, _, pattern = item.partition("=")
        kind = kind.strip()
        if kind not in KINDS:
            raise SystemExit(f"BRAIN_SOURCES: unknown kind {kind!r} (allowed: {', '.join(KINDS)})")
        out.append((kind, pattern.strip()))
    return out


# ----------------------------------------------------------------- frontmatter

def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return v


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML subset: `key: value`, one level of `  key: value` nesting, block scalars (| >).

    Returns (meta, body). Lists and deeper nesting are ignored (never needed by the brain).
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    if lines[0].strip() != "---":
        return {}, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    meta: dict = {}
    parent: str | None = None
    i = 1
    while i < end:
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()
        if stripped.startswith("- "):
            i += 1
            continue
        if ":" not in stripped:
            i += 1
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            block: list[str] = []
            j = i + 1
            while j < end and (not lines[j].strip() or (len(lines[j]) - len(lines[j].lstrip(" "))) > indent):
                block.append(lines[j].strip())
                j += 1
            value = ("\n" if value[0] == "|" else " ").join(b for b in block).strip()
            i = j
        else:
            value = _unquote(value)
            i += 1
        if indent == 0:
            if value == "":
                meta[key] = {}
                parent = key
            else:
                meta[key] = value
                parent = None
        elif parent is not None and isinstance(meta.get(parent), dict):
            meta[parent][key] = value
    body = "\n".join(lines[end + 1:])
    return meta, body


def _as_date(v) -> date | None:
    if not v or not isinstance(v, str):
        return None
    try:
        return date.fromisoformat(v.strip()[:10])
    except ValueError:
        return None


# ----------------------------------------------------------------- chunking

def _join_tokens(tokens: list[str]) -> str:
    out: list[str] = []
    for t in tokens:
        if t.startswith("\n"):
            if out and out[-1] == " ":
                out.pop()
            out.append(t)
        else:
            out.append(t)
            out.append(" ")
    return "".join(out).strip()


def _cost(tok: str) -> int:
    """Estimated model tokens for one whitespace-delimited word (0 for a newline run)."""
    return 0 if tok.startswith("\n") else 1 + len(tok) // 4


def _cost_of(tokens: list[str]) -> int:
    return sum(_cost(t) for t in tokens)


def _tail(tokens: list[str], budget: int) -> list[str]:
    """The trailing tokens worth about `budget` cost (the overlap carried into the next chunk)."""
    out: list[str] = []
    c = 0
    for t in reversed(tokens):
        if c >= budget:
            break
        out.append(t)
        c += _cost(t)
    return list(reversed(out))


def _window(tokens: list[str], size: int, overlap: int) -> list[list[str]]:
    """Split a token list (words and newline runs) into windows of about `size` cost with `overlap`."""
    if _cost_of(tokens) <= size:
        return [tokens]
    out: list[list[str]] = []
    cur: list[str] = []
    c = 0
    for t in tokens:
        if c + _cost(t) > size and cur:
            out.append(cur)
            cur = _tail(cur, overlap) if overlap else []
            c = _cost_of(cur)
        cur.append(t)
        c += _cost(t)
    if cur and _cost_of(cur) > (_cost_of(_tail(out[-1], overlap)) if out else 0):
        out.append(cur)
    return out


def chunk_text(body: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """Headings first: sections are packed into chunks of about `size` words; a section longer
    than `size` is windowed with `overlap`; consecutive chunks share `overlap` words.
    `size` / `overlap` are estimated tokens (see _cost). Defaults CHUNK_TOKENS / CHUNK_OVERLAP
    (env BRAIN_CHUNK_TOKENS / BRAIN_CHUNK_OVERLAP)."""
    size = size or CHUNK_TOKENS
    overlap = CHUNK_OVERLAP if overlap is None else overlap
    sections: list[list[str]] = []
    cur: list[str] = []
    for line in body.splitlines():
        if _HEADING_RE.match(line) and cur:
            sections.append(cur)
            cur = []
        cur.append(line)
    if cur:
        sections.append(cur)

    chunks: list[list[str]] = []
    acc: list[str] = []
    acc_words = 0

    words = _cost_of

    def flush() -> None:
        nonlocal acc, acc_words
        if acc_words:
            chunks.append(acc)
        acc, acc_words = [], 0

    for sec in sections:
        tokens = _TOKEN_RE.findall("\n".join(sec))
        w = words(tokens)
        if w == 0:
            continue
        if w > size:
            flush()
            chunks.extend(_window(tokens, size, overlap))
            continue
        if acc_words and acc_words + w > size:
            flush()
        if not acc and chunks and overlap:
            acc = _tail(chunks[-1], overlap) + ["\n"]
            acc_words = _cost_of(acc)
        acc = acc + (["\n"] if acc else []) + tokens
        acc_words += w
    flush()
    return [t for t in (_join_tokens(c) for c in chunks) if t]


# ----------------------------------------------------------------- pages

@dataclass
class Page:
    path: str
    kind: str
    title: str
    description: str | None
    type: str | None
    since: date | None
    superseded_by: str | None
    checked_last: date | None
    sha: str
    body: str
    chunks: list[str] = field(default_factory=list)


def _rel_path(file: Path, repo: Path) -> str:
    try:
        return file.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return file.resolve().as_posix()


def _title_from(meta: dict, body: str, file: Path) -> str:
    if meta.get("name"):
        return str(meta["name"])
    if meta.get("title"):
        return str(meta["title"])
    for line in body.splitlines():
        if _HEADING_RE.match(line):
            return line.lstrip("#").strip()
    return file.stem


def _meta_get(meta: dict, key: str):
    v = meta.get(key)
    if v in (None, "") and isinstance(meta.get("metadata"), dict):
        v = meta["metadata"].get(key)
    return v if v not in ("", {}) else None


def _finish(p: Page) -> Page:
    p.chunks = chunk_text(p.body) or ([p.title] if p.title else [])
    return p


def page_from_file(file: Path, kind: str, repo: Path) -> Page:
    raw = file.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    title = _title_from(meta, body, file)
    return _finish(Page(
        path=_rel_path(file, repo), kind=kind, title=title,
        description=(str(_meta_get(meta, "description")) if _meta_get(meta, "description") else None),
        type=(str(_meta_get(meta, "type")) if _meta_get(meta, "type") else None),
        since=_as_date(_meta_get(meta, "since")),
        superseded_by=(str(_meta_get(meta, "superseded_by")) if _meta_get(meta, "superseded_by") else None),
        checked_last=_as_date(_meta_get(meta, "checked_last")),
        sha=hashlib.sha1(raw).hexdigest(), body=body.strip(),
    ))


def rule_pages(file: Path, repo: Path) -> list[Page]:
    """A contract file: one page per `| H<n> | rule | source |` row, plus one page for the prose."""
    raw = file.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    meta, body = parse_frontmatter(text)
    base = _rel_path(file, repo)
    stem = file.stem
    pages: list[Page] = []
    prose: list[str] = []
    for line in body.splitlines():
        m = _RULE_ROW_RE.match(line)
        if m and m.group(1) and not m.group(2).strip().startswith("---"):
            rid, rule, source = m.group(1), m.group(2).strip(), m.group(3).strip()
            if rule.lower() in ("rule", "#") or set(rule) <= set("- "):
                continue
            rule_text = f"{rid}: {rule}" + (f"\n(source: {source})" if source else "")
            pages.append(_finish(Page(
                path=f"{base}#{rid}", kind="rule", title=f"{stem} {rid}", description=None,
                type="rule", since=None, superseded_by=None, checked_last=None,
                sha=hashlib.sha1(rule_text.encode("utf-8")).hexdigest(), body=rule_text,
            )))
        else:
            prose.append(line)
    if not pages:
        return [page_from_file(file, "rule", repo)]
    prose_body = "\n".join(prose).strip()
    pages.append(_finish(Page(
        path=base, kind="rule", title=_title_from(meta, body, file), description=None, type="rule",
        since=None, superseded_by=None, checked_last=None,
        sha=hashlib.sha1(prose_body.encode("utf-8")).hexdigest(), body=prose_body,
    )))
    return pages


def collect_pages(sources: Iterable[tuple[str, str]], repo: Path | None = None) -> list[Page]:
    """Precompute the full page list (with chunks) from the configured globs."""
    repo = repo or repo_root()
    pages: list[Page] = []
    seen: set[str] = set()
    for kind, pattern in sources:
        for f in sorted(glob.glob(pattern)):
            file = Path(f)
            if not file.is_file():
                continue
            new = rule_pages(file, repo) if kind == "rule" else [page_from_file(file, kind, repo)]
            for p in new:
                if p.path in seen:
                    continue
                seen.add(p.path)
                pages.append(p)
    return pages


def plan_changes(pages: list[Page], existing: dict[str, tuple[str, bool]]) -> tuple[list[Page], list[str]]:
    """existing: {path: (sha, is_deleted)}. Returns (pages to (re)index, paths to soft-delete)."""
    on_disk = {p.path for p in pages}
    changed = [p for p in pages
               if p.path not in existing or existing[p.path][0] != p.sha or existing[p.path][1]]
    gone = [path for path, (_, is_del) in existing.items() if not is_del and path not in on_disk]
    return changed, gone


# ----------------------------------------------------------------- embeddings

_EMBEDDER: Callable[[list[str]], list[list[float]]] | None = None


def get_embedder() -> Callable[[list[str]], list[list[float]]]:
    """Loads the sentence-transformers model once per process; returns a batch encoder."""
    global _EMBEDDER
    if _EMBEDDER is None:
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        from sentence_transformers import SentenceTransformer  # heavy import, deferred
        model = SentenceTransformer(MODEL_NAME, device="cpu")

        def encode(texts: list[str]) -> list[list[float]]:
            if not texts:
                return []
            arr = model.encode(texts, batch_size=EMBED_BATCH, normalize_embeddings=True,
                               convert_to_numpy=True, show_progress_bar=False)
            return [row.tolist() for row in arr]
        _EMBEDDER = encode
    return _EMBEDDER


def embed_query(text: str) -> list[float]:
    return get_embedder()([f"query: {text}"])[0]


def passage_text(title: str, chunk: str) -> str:
    return f"passage: {title}\n{chunk}"


# ----------------------------------------------------------------- db

def connect(dsn: str | None = None):
    import psycopg
    from pgvector.psycopg import register_vector
    dsn = dsn or os.environ.get("SODA_DB_DSN")
    if not dsn:
        raise SystemExit("SODA_DB_DSN is not set (put it in the repo's .env, see .env.example)")
    conn = psycopg.connect(dsn)
    register_vector(conn)
    return conn


def fetch_existing(conn, kinds: Iterable[str]) -> dict[str, tuple[str, bool]]:
    rows = conn.execute(
        "SELECT path, sha, deleted_at IS NOT NULL FROM brain.pages WHERE kind = ANY(%s)", [list(kinds)]
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def fetch_reusable(conn, paths: list[str]) -> dict[tuple[str, str], list[float]]:
    if not paths:
        return {}
    rows = conn.execute(
        "SELECT p.title, c.text, c.embedding FROM brain.chunks c JOIN brain.pages p ON p.id = c.page_id "
        "WHERE p.path = ANY(%s) AND c.embedding IS NOT NULL", [paths]
    ).fetchall()
    return {(r[0], r[1]): r[2] for r in rows}


def write_page(conn, page: Page, vectors: list) -> None:
    """Upsert one page and replace its chunks, in one transaction."""
    with conn.transaction():
        pid = conn.execute(
            """INSERT INTO brain.pages (path, kind, title, description, type, since, superseded_by,
                                        checked_last, sha, body, updated_at, deleted_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), NULL)
               ON CONFLICT (path) DO UPDATE SET kind=EXCLUDED.kind, title=EXCLUDED.title,
                   description=EXCLUDED.description, type=EXCLUDED.type, since=EXCLUDED.since,
                   superseded_by=EXCLUDED.superseded_by, checked_last=EXCLUDED.checked_last,
                   sha=EXCLUDED.sha, body=EXCLUDED.body, updated_at=now(), deleted_at=NULL
               RETURNING id""",
            [page.path, page.kind, page.title, page.description, page.type, page.since,
             page.superseded_by, page.checked_last, page.sha, page.body],
        ).fetchone()[0]
        conn.execute("DELETE FROM brain.chunks WHERE page_id = %s", [pid])
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO brain.chunks (page_id, ord, text, embedding) VALUES (%s,%s,%s,%s)",
                [(pid, i, text, vec) for i, (text, vec) in enumerate(zip(page.chunks, vectors))],
            )


def soft_delete(conn, paths: list[str]) -> int:
    """Marks pages gone from disk. Their chunks stay (search filters deleted_at; a page that comes
    back reuses its embeddings)."""
    if not paths:
        return 0
    with conn.transaction():
        cur = conn.execute(
            "UPDATE brain.pages SET deleted_at = now() WHERE path = ANY(%s) AND deleted_at IS NULL", [paths])
        return cur.rowcount


def set_meta(conn, k: str, v) -> None:
    from psycopg.types.json import Jsonb
    conn.execute("INSERT INTO brain.meta (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                 [k, Jsonb(v)])
    conn.commit()


def stats(conn) -> dict:
    out: dict = {"pages": {}, "chunks": 0, "embedded": 0, "last_index_at": None}
    for kind, n in conn.execute(
            "SELECT kind, count(*) FROM brain.pages WHERE deleted_at IS NULL GROUP BY kind ORDER BY kind"):
        out["pages"][kind] = n
    out["pages_total"] = sum(out["pages"].values())
    out["pages_deleted"] = conn.execute("SELECT count(*) FROM brain.pages WHERE deleted_at IS NOT NULL").fetchone()[0]
    out["chunks"], out["embedded"] = conn.execute(
        "SELECT count(*), count(c.embedding) FROM brain.chunks c JOIN brain.pages p ON p.id = c.page_id "
        "WHERE p.deleted_at IS NULL").fetchone()
    row = conn.execute("SELECT v FROM brain.meta WHERE k = 'last_index_at'").fetchone()
    out["last_index_at"] = row[0] if row else None
    return out


# ----------------------------------------------------------------- run

def run_index(dsn: str | None = None, sources: list[tuple[str, str]] | None = None, repo: Path | None = None,
              embed: Callable[[list[str]], list[list[float]]] | None = None, log=print) -> dict:
    """Index everything that changed. Returns a summary dict. `embed` defaults to get_embedder()
    (loaded only when something changed)."""
    t0 = time.time()
    repo = repo or repo_root()
    sources = sources or parse_sources(os.environ.get("BRAIN_SOURCES"), repo)
    pages = collect_pages(sources, repo)
    kinds = sorted({k for k, _ in sources})
    conn = connect(dsn)
    try:
        existing = fetch_existing(conn, kinds)
        changed, gone = plan_changes(pages, existing)
        total_chunks = sum(len(p.chunks) for p in changed)
        reused = 0
        embedded = 0
        if changed:
            reusable = fetch_reusable(conn, [p.path for p in changed])
            vectors: dict[int, list] = {}
            todo: list[tuple[int, int, str]] = []
            for pi, p in enumerate(changed):
                vectors[pi] = [None] * len(p.chunks)
                for ci, text in enumerate(p.chunks):
                    vec = reusable.get((p.title, text))
                    if vec is not None:
                        vectors[pi][ci] = vec
                        reused += 1
                    else:
                        todo.append((pi, ci, passage_text(p.title, text)))
            if todo:
                enc = embed or get_embedder()
                for start in range(0, len(todo), EMBED_BATCH):
                    batch = todo[start:start + EMBED_BATCH]
                    for (pi, ci, _), vec in zip(batch, enc([t for _, _, t in batch])):
                        vectors[pi][ci] = vec
                        embedded += 1
            for pi, p in enumerate(changed):
                write_page(conn, p, vectors[pi])
        deleted = soft_delete(conn, gone)
        now = datetime.now(timezone.utc).isoformat()
        set_meta(conn, "last_index_at", now)
        summary = {
            "files": len(pages), "changed": len(changed), "unchanged": len(pages) - len(changed),
            "chunks": total_chunks, "embedded": embedded, "reused": reused, "deleted": deleted,
            "seconds": round(time.time() - t0, 1), "at": now,
        }
        set_meta(conn, "last_index", summary)
        # The to-dos, CRM people, hub cards and CRM review items become rows + vectors in the same pass (the model is
        # loaded once); a failure there never undoes the page index above. BRAIN_TODO=0 turns it off.
        if os.environ.get("BRAIN_TODO", "1") != "0":
            try:
                import todo_ingest
                summary["todo"] = todo_ingest.run(conn, embed or (lambda texts: get_embedder()(texts)), log=lambda s: None)
            except Exception as e:
                conn.rollback()
                summary["todo_error"] = f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        conn.close()
    log(json.dumps(summary))
    return summary


def dry_run(sources: list[tuple[str, str]], repo: Path, dsn: str | None) -> dict:
    pages = collect_pages(sources, repo)
    by_kind: dict[str, int] = {}
    for p in pages:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
    plan = {"sources": [f"{k}={g}" for k, g in sources], "files": len(pages), "by_kind": by_kind,
            "chunks": sum(len(p.chunks) for p in pages), "changed": None, "deleted": None, "db": False}
    if dsn:
        try:
            conn = connect(dsn)
            try:
                changed, gone = plan_changes(pages, fetch_existing(conn, sorted({k for k, _ in sources})))
            finally:
                conn.close()
            plan.update(changed=len(changed), changed_chunks=sum(len(p.chunks) for p in changed),
                        deleted=len(gone), db=True)
        except Exception as e:  # no DB reachable: the plan still prints
            plan["db_error"] = str(e).strip().splitlines()[0]
    if plan["changed"] is None:
        plan["changed"] = len(pages)
        plan["note"] = "no DB: every page counts as changed"
    return plan


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    ap.add_argument("--stats", action="store_true", help="print counts from the DB")
    ap.add_argument("--repo", help="markdown root (default BRAIN_REPO or <repo>/examples/brain)")
    ap.add_argument("--sources", help="kind=glob,... (default BRAIN_SOURCES or the built-in list)")
    ap.add_argument("--dsn", help="override SODA_DB_DSN")
    a = ap.parse_args(argv)
    load_env()
    repo = Path(a.repo) if a.repo else repo_root()
    sources = parse_sources(a.sources or os.environ.get("BRAIN_SOURCES"), repo)
    dsn = a.dsn or os.environ.get("SODA_DB_DSN")
    if a.stats:
        conn = connect(dsn)
        try:
            print(json.dumps(stats(conn), indent=2, default=str))
        finally:
            conn.close()
        return 0
    if a.dry_run:
        print(json.dumps(dry_run(sources, repo, dsn), indent=2))
        return 0
    run_index(dsn, sources, repo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
