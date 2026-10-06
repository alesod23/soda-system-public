'use strict';

/*
 * Per-source debounce / pooling engine for the notification daemon.
 *
 * Spec: when a message arrives from a source, DON'T process
 * immediately; start a quiet timer of `windowMs`. Each further message from
 * that SAME source resets the timer (pools back-to-back bursts). When the source
 * has been silent for `windowMs`, flush the whole pooled batch as ONE
 * notification. Sources are independent (a burst on one channel does not touch another channel's timer).
 *
 * Zero deps. `windowMs` is injectable so tests can run with a tiny window.
 */

class Debouncer {
  /**
   * @param {number} windowMs  quiet period before a source flushes (e.g. 8000)
   * @param {(source: string, batch: any[]) => void} onFlush
   */
  constructor(windowMs, onFlush, maxWaitMs) {
    this.windowMs = windowMs;
    this.onFlush = onFlush;
    this.maxWaitMs = maxWaitMs || 0; // hard cap on total wait from the FIRST msg; 0 = uncapped
    this.batches = new Map(); // source -> message[]
    this.timers = new Map(); // source -> timeout handle
    this.firstAt = new Map(); // source -> ms of the first message in the current batch
  }

  /** Add a message from `source`; (re)arm the quiet timer, but never past the max-wait cap. */
  add(source, message) {
    if (!this.batches.has(source)) {
      this.batches.set(source, []);
      this.firstAt.set(source, Date.now());
    }
    this.batches.get(source).push(message);

    const existing = this.timers.get(source);
    if (existing) clearTimeout(existing);

    let delay = this.windowMs;
    if (this.maxWaitMs > 0) {
      const remainingCap = this.maxWaitMs - (Date.now() - this.firstAt.get(source));
      if (remainingCap <= 0) { this._flush(source); return; } // cap hit -> flush now
      delay = Math.min(this.windowMs, remainingCap);
    }

    const t = setTimeout(() => this._flush(source), delay);
    if (t.unref) t.unref(); // don't keep the event loop alive on this alone
    this.timers.set(source, t);
  }

  _flush(source) {
    const batch = this.batches.get(source) || [];
    this.batches.delete(source);
    this.firstAt.delete(source);
    const t = this.timers.get(source);
    if (t) clearTimeout(t);
    this.timers.delete(source);
    if (batch.length) {
      try {
        this.onFlush(source, batch);
      } catch (e) {
        // never let a flush handler crash the daemon
        // eslint-disable-next-line no-console
        console.error(`[debounce] onFlush(${source}) threw: ${e && e.message}`);
      }
    }
  }

  /** Force-flush every pending source (e.g. on shutdown). */
  flushAll() {
    for (const source of [...this.timers.keys()]) this._flush(source);
  }

  /** How many sources currently have a pending (unflushed) batch. */
  pendingSources() {
    return this.batches.size;
  }
}

module.exports = { Debouncer };
