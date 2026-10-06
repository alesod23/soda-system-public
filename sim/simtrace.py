"""simtrace.py: observability for a simulation run.

Every span is always written to <run>/trace.jsonl (local, offline). When LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are
set (LANGFUSE_HOST optional) and the `langfuse` Python SDK is importable, the same spans also go to Langfuse: one trace
per simulated day, a span per event and per pass, a generation per model call. Any Langfuse failure turns it off for
the rest of the run (one warning); the simulation never fails because of observability.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path


class Tracer:
    def __init__(self, run_dir: Path, run: str, arm: str):
        self.file = run_dir / "trace.jsonl"
        self.run, self.arm = run, arm
        self.lf = None
        self._trace = None
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
            try:
                from langfuse import Langfuse  # optional dependency
                self.lf = Langfuse(public_key=os.environ["LANGFUSE_PUBLIC_KEY"], secret_key=os.environ["LANGFUSE_SECRET_KEY"],
                                   host=os.environ.get("LANGFUSE_HOST") or None)
            except Exception as e:  # noqa: BLE001 (not installed, wrong version, bad keys)
                self._off(e)

    @property
    def enabled(self) -> bool:
        return self.lf is not None

    def _off(self, e):
        if self.lf is not None or not hasattr(self, "_warned"):
            sys.stderr.write(f"langfuse off for this run: {type(e).__name__}: {str(e)[:120]}\n")
            self._warned = True
        self.lf = None

    def _local(self, kind, name, **kw):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "kind": kind, "name": name, "run": self.run, "arm": self.arm, **kw},
                               ensure_ascii=False, default=str) + "\n")

    def day(self, name: str, **meta):
        """Start the trace of one simulated day."""
        self._local("day", name, **meta)
        self._trace = None
        if self.lf:
            try:
                if hasattr(self.lf, "trace"):            # SDK v2
                    self._trace = self.lf.trace(name=name, session_id=self.run, metadata={"arm": self.arm, **meta})
                else:                                     # SDK v3: a root span stands in for the trace
                    self._trace = self.lf.start_span(name=name, metadata={"run": self.run, "arm": self.arm, **meta})
            except Exception as e:  # noqa: BLE001
                self._off(e)

    @contextmanager
    def span(self, name: str, **meta):
        t0 = time.time()
        out: dict = {}
        sp = None
        if self._trace is not None:
            try:
                sp = self._trace.span(name=name, metadata=meta) if hasattr(self._trace, "span") else \
                    self._trace.start_span(name=name, metadata=meta)
            except Exception as e:  # noqa: BLE001
                self._off(e)
        try:
            yield out
        finally:
            self._local("span", name, ms=int((time.time() - t0) * 1000), **meta, **out)
            if sp is not None:
                try:
                    try:
                        sp.end(output=out)            # SDK v2
                    except TypeError:
                        sp.update(output=out)         # SDK v3
                        sp.end()
                except Exception as e:  # noqa: BLE001
                    self._off(e)

    def generation(self, name: str, prompt: str, answer: str, model: str | None, ms: int):
        self._local("generation", name, model=model, ms=ms, prompt_chars=len(prompt), answer_chars=len(answer or ""))
        if self._trace is not None:
            try:
                if hasattr(self._trace, "generation"):
                    self._trace.generation(name=name, model=model, input=prompt[:4000], output=(answer or "")[:4000])
                else:
                    g = self._trace.start_generation(name=name, model=model, input=prompt[:4000])
                    g.update(output=(answer or "")[:4000]); g.end()
            except Exception as e:  # noqa: BLE001
                self._off(e)

    def flush(self):
        if self.lf:
            try:
                if self._trace is not None and not hasattr(self.lf, "trace"):
                    self._trace.end()
                self.lf.flush()
            except Exception as e:  # noqa: BLE001
                self._off(e)
