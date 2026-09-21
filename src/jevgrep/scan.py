"""Streaming pipeline: read lines, micro-batch them, judge each batch, yield verdicts in order."""

from __future__ import annotations

import codecs
import os
import queue
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TextIO

from jevgrep.judge import Judge, JudgeError

MAX_LINE_CHARS = 500  # longer lines are truncated before judging (output keeps the full line)
IDLE_FLUSH = 0.3  # send a partial batch after this long without new input
MAX_BATCH_WAIT = 1.0  # ...or once its oldest line has waited this long (keeps a trickle live)
POLL_INTERVAL = 0.05  # how often to check for finished requests while waiting on input
CACHE_SIZE = 100_000


@dataclass(frozen=True)
class Record:
    line_no: int
    text: str
    source: str | None = None  # file name, set only when scanning several inputs


@dataclass(frozen=True)
class Verdict:
    record: Record
    probability: float
    selected: bool  # passes the filter: at/above the threshold, or below it with --invert


@dataclass
class ScanCounts:
    read: int = 0
    empty: int = 0
    judged: int = 0  # lines sent to the judge
    cached: int = 0  # lines answered from the cache or an identical line in flight
    selected: int = 0


# --- input -------------------------------------------------------------------------------------


def read_records(paths: Sequence[str], stdin: TextIO) -> Iterator[Record]:
    """Yield input lines from files (`-` is stdin), or from stdin when no files are given."""
    label = len(paths) > 1
    for path in paths or ["-"]:
        if path == "-":
            source = "(standard input)" if label else None
            yield from _records(_stdin_lines(stdin), source)
        else:
            with open(path, encoding="utf-8", errors="replace") as stream:
                yield from _records(iter(stream.readline, ""), path if label else None)


def _records(lines: Iterable[str], source: str | None) -> Iterator[Record]:
    for line_no, line in enumerate(lines, start=1):
        yield Record(line_no, line.rstrip("\r\n"), source)


def _stdin_lines(stream: TextIO) -> Iterator[str]:
    """Read stdin straight from its file descriptor.

    The reader runs in a daemon thread that may still be blocked on input when we exit (Ctrl+C,
    `| head`). Going through Python's buffered stdin there can abort the interpreter at shutdown
    ("could not acquire lock for <stdin>"); raw os.read holds no such lock.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):  # in-memory streams, e.g. in tests
        yield from iter(stream.readline, "")
        return
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while chunk := os.read(fd, 65536):
        pending += decoder.decode(chunk)
        *lines, pending = pending.split("\n")
        yield from lines
    pending += decoder.decode(b"", final=True)
    if pending:
        yield pending


# --- batching ----------------------------------------------------------------------------------


_EOF = object()


@dataclass(frozen=True)
class _ReadFailure:
    error: BaseException


def _pump(records: Iterable[Record], lines: queue.Queue) -> None:
    try:
        for record in records:
            lines.put(record)
    except Exception as exc:  # hand read errors to the main thread
        lines.put(_ReadFailure(exc))
    else:
        lines.put(_EOF)


class Batcher:
    """Groups queued lines into batches.

    A batch is sent when it reaches `size` lines, after `idle` seconds without new input, or when
    its oldest line has waited `max_wait` seconds, whichever comes first.
    """

    def __init__(
        self,
        lines: queue.Queue,
        size: int,
        idle: float = IDLE_FLUSH,
        max_wait: float = MAX_BATCH_WAIT,
    ) -> None:
        self.lines = lines
        self.size = size
        self.idle = idle
        self.max_wait = max_wait
        self.pending: list[Record] = []
        self.eof = False
        self._first = self._last = 0.0

    @property
    def drained(self) -> bool:
        return self.eof and not self.pending

    def poll(self, timeout: float | None) -> list[Record] | None:
        """Wait up to `timeout` s (None: until input arrives); return a batch if one is due."""
        if self.pending:
            wait = max(0.0, self._due() - time.monotonic())
            timeout = wait if timeout is None else min(timeout, wait)
        try:
            item = self.lines.get(timeout=timeout)
        except queue.Empty:
            item = None
        now = time.monotonic()
        if item is _EOF:
            self.eof = True
        elif isinstance(item, _ReadFailure):
            raise item.error
        elif item is not None:
            if not self.pending:
                self._first = now
            self.pending.append(item)
            self._last = now

        if self.pending and (self.eof or len(self.pending) >= self.size or now >= self._due()):
            batch, self.pending = self.pending, []
            return batch
        return None

    def _due(self) -> float:
        return min(self._last + self.idle, self._first + self.max_wait)


# --- judging -----------------------------------------------------------------------------------


@dataclass(eq=False)
class _Batch:
    records: list[Record] = field(default_factory=list)  # non-empty lines, in input order
    keys: list[str] = field(default_factory=list)  # the (truncated) text judged for each record
    send: list[str] = field(default_factory=list)  # unique keys this batch asks the judge about
    known: dict[str, float] = field(default_factory=dict)  # cache hits
    borrowed: dict[str, _Batch] = field(default_factory=dict)  # keys an earlier batch is judging
    results: dict[str, float] = field(default_factory=dict)
    future: Future = field(default_factory=Future)


class Scanner:
    """Judges a stream of records and yields a Verdict for every non-empty line, in input order.

    Up to `jobs` batches are in flight at once. Identical lines are judged once: later copies
    are answered from an LRU cache, or from the in-flight batch that is already judging them.
    """

    def __init__(
        self,
        judge: Judge,
        question: str,
        *,
        threshold: float = 0.5,
        invert: bool = False,
        batch_size: int = 20,
        jobs: int = 4,
        idle: float = IDLE_FLUSH,
        max_wait: float = MAX_BATCH_WAIT,
        cache_size: int = CACHE_SIZE,
    ) -> None:
        self.judge = judge
        self.question = question
        self.threshold = threshold
        self.invert = invert
        self.batch_size = batch_size
        self.jobs = jobs
        self.idle = idle
        self.max_wait = max_wait
        self.cache_size = cache_size
        self.counts = ScanCounts()
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._judging: dict[str, _Batch] = {}

    def scan(self, records: Iterable[Record]) -> Iterator[Verdict]:
        lines: queue.Queue = queue.Queue(maxsize=max(1000, 4 * self.batch_size * self.jobs))
        threading.Thread(target=_pump, args=(records, lines), daemon=True).start()
        batcher = Batcher(lines, self.batch_size, self.idle, self.max_wait)
        inflight: deque[_Batch] = deque()
        while True:
            batch = batcher.poll(POLL_INTERVAL if inflight else None)
            if batch:
                inflight.append(self._submit(batch))
            while inflight and (
                inflight[0].future.done() or len(inflight) >= self.jobs or batcher.drained
            ):
                yield from self._finish(inflight.popleft())
            if batcher.drained and not inflight:
                return

    def _submit(self, records: list[Record]) -> _Batch:
        batch = _Batch()
        for record in records:
            self.counts.read += 1
            if not record.text.strip():
                self.counts.empty += 1
                continue
            batch.records.append(record)
            batch.keys.append(record.text[:MAX_LINE_CHARS])

        for key in dict.fromkeys(batch.keys):
            if key in self._cache:
                self._cache.move_to_end(key)
                batch.known[key] = self._cache[key]
            elif key in self._judging:
                batch.borrowed[key] = self._judging[key]
            else:
                batch.send.append(key)
                self._judging[key] = batch
        self.counts.judged += len(batch.send)
        self.counts.cached += len(batch.records) - len(batch.send)

        if batch.send:
            # Daemon threads rather than a ThreadPoolExecutor: on Ctrl+C or a closed pipe we can
            # exit at once instead of waiting for requests that are still in flight.
            threading.Thread(target=self._judge_batch, args=(batch,), daemon=True).start()
        else:
            batch.future.set_result([])
        return batch

    def _judge_batch(self, batch: _Batch) -> None:
        try:
            batch.future.set_result(self.judge.judge(batch.send, self.question))
        except BaseException as exc:  # re-raised in the main thread by _finish
            batch.future.set_exception(exc)

    def _finish(self, batch: _Batch) -> Iterator[Verdict]:
        probabilities = batch.future.result()
        if len(probabilities) != len(batch.send):
            raise JudgeError(
                f"judge returned {len(probabilities)} answers for {len(batch.send)} lines"
            )
        batch.results = dict(zip(batch.send, probabilities, strict=True))
        for key, probability in batch.results.items():
            self._remember(key, probability)
            if self._judging.get(key) is batch:
                del self._judging[key]

        # Batches finish in submission order, so every borrowed batch already has its results.
        values = {key: owner.results[key] for key, owner in batch.borrowed.items()}
        values.update(batch.known)
        values.update(batch.results)
        for record, key in zip(batch.records, batch.keys, strict=True):
            probability = values[key]
            selected = (probability >= self.threshold) != self.invert
            self.counts.selected += selected
            yield Verdict(record, probability, selected)

    def _remember(self, key: str, probability: float) -> None:
        self._cache[key] = probability
        self._cache.move_to_end(key)
        if len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
