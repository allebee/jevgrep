"""Formatting for matched lines, JSONL records and the --stats report."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass

from jevgrep.judge import JEV_USD_PER_MTOK, Usage
from jevgrep.scan import ScanCounts, Verdict


def format_line(verdict: Verdict, *, line_number: bool = False, score: bool = False) -> str:
    """grep-style: optional `file:`, `line:` and `probability:` prefixes before the line."""
    parts: list[str] = []
    if verdict.record.source is not None:
        parts.append(verdict.record.source)
    if line_number:
        parts.append(str(verdict.record.line_no))
    if score:
        parts.append(f"{verdict.probability:.2f}")
    parts.append(verdict.record.text)
    return ":".join(parts)


def format_json(verdict: Verdict) -> str:
    record = verdict.record
    obj: dict[str, object] = {} if record.source is None else {"file": record.source}
    obj.update(
        line_no=record.line_no,
        text=record.text,
        probability=round(verdict.probability, 4),
        matched=verdict.selected,
    )
    return json.dumps(obj, ensure_ascii=False)


@dataclass
class ExplainCost:
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


def format_stats(
    counts: ScanCounts,
    usage: Usage | None,
    elapsed: float,
    *,
    route: str,
    explain: ExplainCost | None = None,
) -> str:
    rows = [
        ("lines scanned", f"{counts.read:,}"),
        ("lines matched", f"{counts.selected:,}"),
        ("judged", f"{counts.judged:,} ({counts.cached:,} from cache, {counts.empty:,} empty)"),
    ]
    if usage is not None:
        rows += [
            ("requests", f"{usage.requests:,}"),
            ("input tokens", f"{usage.input_tokens:,}"),
            ("cost", f"${usage.cost_usd:.6f} {_cost_source(usage)}"),
        ]
    rows.append(("elapsed", f"{elapsed:.2f} s"))
    if usage is not None and usage.latencies:
        rows.append(("p50 latency", f"{statistics.median(usage.latencies):.3f} s per request"))
    models = ", ".join(sorted(usage.models)) if usage is not None and usage.models else None
    rows.append(("model", f"{route}" + (f" (answered by {models})" if models else "")))
    if explain is not None:
        tokens = (
            f"{explain.input_tokens:,} in / {explain.output_tokens:,} out"
            if explain.input_tokens is not None and explain.output_tokens is not None
            else "tokens n/a"
        )
        cost = f"${explain.cost_usd:.6f}" if explain.cost_usd is not None else "cost n/a"
        rows.append(("explain", f"{explain.model}, {tokens}, {cost}"))

    width = max(len(name) for name, _ in rows)
    return "\n".join(["jevgrep stats:", *(f"  {name:<{width}}  {value}" for name, value in rows)])


def _cost_source(usage: Usage) -> str:
    if usage.estimated_cost_requests == 0:
        return "(reported by provider)"
    estimate = f"estimated at ${JEV_USD_PER_MTOK}/Mtok"
    if usage.estimated_cost_requests == usage.requests:
        return f"({estimate})"
    return f"(partly {estimate})"
