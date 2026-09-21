"""Benchmark jevgrep's Jev judge against LLMs on the hand-labelled sample, all through OpenRouter.

    uv sync --group bench
    OPENROUTER_API_KEY=... uv run python bench/bench.py

Every system gets the same per-line Noul questions, the same state ({"lines": {...}}), the same
batching and cache (jevgrep's Scanner) and the same 0.5 threshold. The LLMs answer through the
official system-one-adapter package, which turns the Noul questions into an LLM prompt and parses
probabilities back. Requests run one at a time (--jobs 1) so wall times are comparable.

Writes bench/results.md (tables) and bench/results.json (every probability, for error analysis).
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date
from importlib.metadata import version
from pathlib import Path
from typing import Any

from system_one_adapter import SystemOneAdapterClient
from system_one_adapter.providers.openai import OpenAIProvider

from jevgrep.judge import RETRY_POLICY, SystemOneJudge, make_jev_judge
from jevgrep.provider import OPENROUTER_CHAT_BASE_URL, resolve_provider
from jevgrep.scan import Scanner, read_records

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "examples" / "sample.log"
LABELS = ROOT / "examples" / "labels.csv"
THRESHOLD = 0.5

TASKS = {
    "real_error": "This line reports a real error, not routine noise",
    "payment_failure": "Mentions a payment failure",
    "slow_request": "Reports a request that took longer than 2 seconds",
}


@dataclass(frozen=True)
class System:
    name: str
    kind: str  # "jev" or "llm"
    model: str
    batch_size: int = 20
    layout: str = "keyed"  # see jevgrep.judge.build_request


SYSTEMS = {
    "jev": System("Jev (batch 20)", "jev", "jev-1.13"),
    "jev-list": System("Jev (batch 20, list layout)", "jev", "jev-1.13", layout="list"),
    "jev-b1": System("Jev (batch 1)", "jev", "jev-1.13", batch_size=1),
    "haiku": System("Claude Haiku 4.5", "llm", "anthropic/claude-haiku-4.5"),
    "sonnet": System("Claude Sonnet 5", "llm", "anthropic/claude-sonnet-5"),
}


class AdapterJudge(SystemOneJudge):
    """The same per-line Nouls, answered by an LLM through system-one-adapter."""

    def reported_cost(self, response: Any) -> float | None:
        """Sum OpenRouter's `usage.cost` over every LLM call, including corrective retries."""
        total = 0.0
        for attempt in response.debug.get("llm_attempts", []):
            usage = (attempt.get("llm_response") or {}).get("usage") or {}
            if not isinstance(usage.get("cost"), (int, float)):
                return None
            total += usage["cost"]
        return total


def make_judge(system: System, api_key: str, structured: bool) -> SystemOneJudge:
    if system.kind == "jev":
        return make_jev_judge(resolve_provider("openrouter"), system.model, system.layout)
    client = SystemOneAdapterClient(
        structured_outputs=structured,
        llm_answer_mode="probabilities",
        normalize_probabilities=True,
        n_retry_malformed_structure=2,
        retry=dataclasses.replace(RETRY_POLICY, timeout=180.0),  # slower models need longer
    )
    provider = OpenAIProvider(
        system.model, base_url=OPENROUTER_CHAT_BASE_URL, api_key=api_key, api="chat_completions"
    )
    return AdapterJudge(client, model=provider, layout=system.layout)


def load_labels() -> dict[int, dict[str, int]]:
    lines = SAMPLE.read_text(encoding="utf-8").splitlines()
    labels = {}
    with LABELS.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            line_no = int(row["line_no"])
            assert lines[line_no - 1] == row["text"], f"labels.csv is out of sync at {line_no}"
            labels[line_no] = {task: int(row[task]) for task in TASKS}
    return labels


def score(probabilities: dict[int, float], truth: dict[int, int]) -> dict[str, float]:
    tp = sum(probabilities[n] >= THRESHOLD and truth[n] == 1 for n in truth)
    fp = sum(probabilities[n] >= THRESHOLD and truth[n] == 0 for n in truth)
    fn = sum(probabilities[n] < THRESHOLD and truth[n] == 1 for n in truth)
    tn = len(truth) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "accuracy": (tp + tn) / len(truth),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
    }


def run(system: System, task: str, api_key: str, structured: bool) -> dict[str, Any]:
    judge = make_judge(system, api_key, structured)
    scanner = Scanner(judge, TASKS[task], threshold=THRESHOLD, batch_size=system.batch_size, jobs=1)
    started = time.perf_counter()
    verdicts = list(scanner.scan(read_records([str(SAMPLE)], sys.stdin)))
    elapsed = time.perf_counter() - started
    usage = judge.usage
    return {
        "system": system.name,
        "task": task,
        "question": TASKS[task],
        "requested_model": system.model,
        "answered_by": sorted(usage.models),
        "batch_size": system.batch_size,
        "layout": system.layout,
        "seconds": elapsed,
        "requests": usage.requests,
        "input_tokens": usage.input_tokens,
        "cost_usd": usage.cost_usd if usage.estimated_cost_requests == 0 else None,
        "p50_latency": statistics.median(usage.latencies) if usage.latencies else None,
        "probabilities": {v.record.line_no: v.probability for v in verdicts},
    }


# --- report ------------------------------------------------------------------------------------


def money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def report(runs: list[dict[str, Any]], labels: dict[int, dict[str, int]], meta: dict) -> str:
    out = [
        "# Benchmark results",
        "",
        f"Run on {meta['date']} through **{meta['provider']}**. "
        f"Jev pinned to `{meta['jev_model']}` "
        f"(answered by `{meta['jev_answered_by']}`). {meta['lines']} labelled lines of "
        f"`examples/sample.log`, threshold {THRESHOLD}, requests sent one at a time. "
        f"LLMs via system-one-adapter {meta['adapter_version']} "
        f"({'native structured output' if meta['structured'] else 'prompted JSON'}, "
        "probabilities mode). Cost is OpenRouter's reported `usage.cost`.",
        "",
        "Every system gets the same questions. Unless a row says otherwise, lines are keyed "
        '(`state={"lines": {"line_01": ...}}`, "Does `lines.line_01` satisfy: ...?"); the '
        '*list layout* row uses the original `state={"lines": [...]}` with `lines[i]`.',
        "",
    ]
    for task in dict.fromkeys(r["task"] for r in runs):
        question = TASKS[task]
        positives = sum(label[task] for label in labels.values())
        out += [
            f'## `{task}`: "{question}" ({positives} positives)',
            "",
            "| System | Accuracy | Precision | Recall | F1 | Time | Requests | p50 | Cost |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in (r for r in runs if r["task"] == task):
            m = r["metrics"]
            out.append(
                f"| {r['system']} | {m['accuracy']:.3f} | {m['precision']:.3f} | {m['recall']:.3f} "
                f"| {m['f1']:.3f} | {r['seconds']:.1f} s | {r['requests']} "
                f"| {r['p50_latency']:.2f} s | {money(r['cost_usd'])} |"
            )
        out.append("")

    out += [
        "## All questions",
        "",
        "| System | Mean F1 | Total time | Total cost | Cost per 1,000 lines |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in dict.fromkeys(r["system"] for r in runs):
        mine = [r for r in runs if r["system"] == name]
        costs = [r["cost_usd"] for r in mine]
        total = None if None in costs else sum(costs)
        per_k = None if total is None else total / (len(labels) * len(mine)) * 1000
        out.append(
            f"| {name} | {statistics.mean(r['metrics']['f1'] for r in mine):.3f} "
            f"| {sum(r['seconds'] for r in mine):.1f} s | {money(total)} | {money(per_k)} |"
        )
    out.append("")

    jev = [r for r in runs if r["system"] == SYSTEMS["jev"].name]
    lines = SAMPLE.read_text(encoding="utf-8").splitlines()
    if jev:
        out += [f"## Where {SYSTEMS['jev'].name} is wrong", ""]
        others = [n for n in dict.fromkeys(r["system"] for r in runs) if n != jev[0]["system"]]
        for r in jev:
            task = r["task"]
            wrong = [
                n
                for n, p in r["probabilities"].items()
                if (p >= THRESHOLD) != bool(labels[n][task])
            ]
            out += [
                f"### `{task}`: {len(wrong)} errors",
                "",
                "| Line | Label | Jev | " + " | ".join(others) + " | Text |",
                "|---:|---:|---:|" + "---:|" * len(others) + "---|",
            ]
            for n in wrong:
                cells = []
                for other in others:
                    match = [o for o in runs if o["system"] == other and o["task"] == task]
                    cells.append(f"{match[0]['probabilities'][n]:.2f}" if match else "")
                text = lines[n - 1][:110].replace("|", "\\|")
                out.append(
                    f"| {n} | {labels[n][task]} | {r['probabilities'][n]:.2f} | "
                    + " | ".join(cells)
                    + f" | `{text}` |"
                )
            out.append("")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--systems", default=",".join(SYSTEMS), help="comma-separated keys")
    parser.add_argument("--tasks", default=",".join(TASKS), help="comma-separated label columns")
    parser.add_argument("--structured", action="store_true", help="LLM native structured output")
    parser.add_argument("--out", type=Path, default=ROOT / "bench")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        sys.exit("bench: set OPENROUTER_API_KEY")
    labels = load_labels()
    runs = []
    for key in args.systems.split(","):
        system = SYSTEMS[key]
        for task in args.tasks.split(","):
            print(f"{system.name:<20} {task:<16}", end=" ", flush=True, file=sys.stderr)
            result = run(system, task, api_key, args.structured)
            result["metrics"] = score(result["probabilities"], {n: labels[n][task] for n in labels})
            runs.append(result)
            print(
                f"F1 {result['metrics']['f1']:.3f}  {result['seconds']:.1f} s  "
                f"{money(result['cost_usd'])}",
                file=sys.stderr,
            )

    jev_runs = [r for r in runs if r["requested_model"].startswith("jev")]
    meta = {
        "date": date.today().isoformat(),
        "provider": "OpenRouter",
        "jev_model": SYSTEMS["jev"].model,
        "jev_answered_by": ", ".join(sorted({m for r in jev_runs for m in r["answered_by"]})),
        "lines": len(labels),
        "threshold": THRESHOLD,
        "structured": args.structured,
        "jevgrep_version": version("jevgrep"),
        "typesafe_sdk_version": version("typesafe-sdk"),
        "adapter_version": version("system-one-adapter"),
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "results.md").write_text(report(runs, labels, meta) + "\n", encoding="utf-8")
    (args.out / "results.json").write_text(
        json.dumps({"meta": meta, "runs": runs}, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.out / 'results.md'} and results.json", file=sys.stderr)


if __name__ == "__main__":
    main()
