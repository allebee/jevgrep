"""The Judge interface and its Jev implementation on top of typesafe-sdk."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from typesafe_sdk import Noul, RetryPolicy, TypeSafeClient, TypeSafeError

from jevgrep.provider import Provider

# Jev bills input tokens only. Checked 2026-09-21: docs.typesafe.ai/models lists $0.042 per
# million tokens, and OpenRouter's typesafe/jev-1.13 endpoint charges $0.000000042 per token.
JEV_USD_PER_MTOK = 0.042

# Jev reads at most 32K tokens of state plus the longest question. Oversized batches are split
# so the state stays far below that, even for scripts where a character costs about one token.
MAX_STATE_CHARS = 16_000

# Exponential backoff on timeouts, rate limits and server errors, handled inside the SDK.
RETRY_POLICY = RetryPolicy(
    max_retries=4,
    backoff_initial=0.5,
    backoff_max=8.0,
    http_statuses={408, 429, *range(500, 600)},
    timeout=60.0,
)
REQUEST_TIMEOUT = 30.0


class Judge(Protocol):
    def judge(self, lines: Sequence[str], question: str) -> list[float]:
        """Return the probability that each line satisfies `question`, in input order."""
        ...


class JudgeError(Exception):
    """The judge returned something unusable, such as a missing answer."""


def noul_instructions(index: int, question: str) -> str:
    """The literal per-line question. It names the line by index; the line text stays in state."""
    return f"Does `lines[{index}]` satisfy: {question.strip().rstrip('?.! ')}?"


@dataclass
class Usage:
    """Totals across every request a judge has sent."""

    requests: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    estimated_cost_requests: int = 0  # requests without a provider-reported cost
    latencies: list[float] = field(default_factory=list)
    models: set[str] = field(default_factory=set)  # versioned model IDs that answered


class SystemOneJudge:
    """Asks one Noul per line in a single `system_one` request, with state={"lines": [...]}.

    `client` is normally a TypeSafeClient talking to Jev, but anything with the same
    `system_one` signature works, e.g. system-one-adapter's LLM-backed client (see bench/).
    """

    def __init__(
        self, client: Any, model: Any = None, *, max_state_chars: int = MAX_STATE_CHARS
    ) -> None:
        self.client = client
        self.model = model  # None uses the client's default model
        self.max_state_chars = max_state_chars
        self.usage = Usage()
        self._lock = threading.Lock()

    def judge(self, lines: Sequence[str], question: str) -> list[float]:
        probabilities: list[float] = []
        for chunk in _chunks(lines, self.max_state_chars):
            probabilities.extend(self._request(chunk, question))
        return probabilities

    def _request(self, lines: list[str], question: str) -> list[float]:
        questions = {
            f"line_{i}": Noul(instructions=noul_instructions(i, question))
            for i in range(len(lines))
        }
        started = time.perf_counter()
        response = self.client.system_one(
            state={"lines": lines}, questions=questions, model=self.model
        )
        latency = time.perf_counter() - started

        answers = response.nouls
        missing = [name for name in questions if name not in answers]
        if missing:
            raise JudgeError(f"response is missing {len(missing)} of {len(questions)} answers")

        tokens = response.usage.input_tokens or 0
        cost = self.reported_cost(response)
        with self._lock:
            usage = self.usage
            usage.requests += 1
            usage.input_tokens += tokens
            usage.latencies.append(latency)
            usage.models.add(response.model)
            if cost is None:
                usage.cost_usd += tokens * JEV_USD_PER_MTOK / 1_000_000
                usage.estimated_cost_requests += 1
            else:
                usage.cost_usd += cost
        return [answers[name].noul for name in questions]

    def reported_cost(self, response: Any) -> float | None:
        """The USD cost OpenRouter adds as `usage.cost`, or None if the provider doesn't report it.

        The SDK's Usage model drops unknown fields, so read it from the raw response body.
        """
        try:
            cost = response.raw_http_response.json()["usage"]["cost"]
        except (TypeSafeError, ValueError, KeyError, TypeError):
            return None
        return float(cost) if isinstance(cost, (int, float)) else None


def _chunks(lines: Sequence[str], max_chars: int) -> Iterator[list[str]]:
    chunk: list[str] = []
    size = 0
    for line in lines:
        if chunk and size + len(line) > max_chars:
            yield chunk
            chunk, size = [], 0
        chunk.append(line)
        size += len(line)
    if chunk:
        yield chunk


def make_jev_judge(provider: Provider, model: str) -> SystemOneJudge:
    client = TypeSafeClient(
        api_key=provider.api_key,
        base_url=provider.base_url,
        model=model,
        retry=RETRY_POLICY,
        timeout=REQUEST_TIMEOUT,
    )
    return SystemOneJudge(client)
