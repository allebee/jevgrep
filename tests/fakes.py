"""Test doubles: no network anywhere in the test suite."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence


def keyword_score(line: str) -> float:
    if "ERROR" in line:
        return 0.9
    if "WARN" in line:
        return 0.5
    return 0.1


class FakeJudge:
    """Scores lines with `score` and records every batch it was asked about."""

    def __init__(
        self,
        score: Callable[[str], float] = keyword_score,
        *,
        delay: Callable[[Sequence[str]], float] | float = 0.0,
        error: Exception | None = None,
    ) -> None:
        self.score = score
        self.delay = delay
        self.error = error
        self.calls: list[list[str]] = []
        self.questions: list[str] = []
        self._lock = threading.Lock()

    def judge(self, lines: Sequence[str], question: str) -> list[float]:
        with self._lock:
            self.calls.append(list(lines))
            self.questions.append(question)
        delay = self.delay(lines) if callable(self.delay) else self.delay
        if delay:
            time.sleep(delay)
        if self.error is not None:
            raise self.error
        return [self.score(line) for line in lines]

    @property
    def judged(self) -> list[str]:
        return [line for call in self.calls for line in call]
