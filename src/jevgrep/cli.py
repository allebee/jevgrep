"""Command-line interface: flags, output, grep-style exit codes, Ctrl+C and broken pipes."""

from __future__ import annotations

import os
import sys
import time
from typing import TextIO

import click
from typesafe_sdk import TypeSafeAuthenticationError, TypeSafeError

from jevgrep import __version__
from jevgrep import explain as explaining
from jevgrep.judge import Judge, make_jev_judge
from jevgrep.output import format_json, format_line, format_stats
from jevgrep.provider import PROVIDERS, ConfigError, Provider, resolve_provider
from jevgrep.scan import Scanner, read_records

EXIT_MATCH = 0
EXIT_NO_MATCH = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130  # 128 + SIGINT, what a shell reports for a grep killed by Ctrl+C


def make_judge(provider: Provider, model: str) -> Judge:
    """Build the Jev judge. Tests swap this for a fake."""
    return make_jev_judge(provider, model)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("question")
@click.argument("files", nargs=-1, type=click.Path(exists=True, dir_okay=False, allow_dash=True))
@click.option(
    "-t",
    "--threshold",
    type=click.FloatRange(0.0, 1.0),
    default=0.5,
    show_default=True,
    help="Select lines whose probability is at or above this.",
)
@click.option("-v", "--invert", is_flag=True, help="Select lines below the threshold instead.")
@click.option("-n", "--line-number", is_flag=True, help="Prefix each line with its line number.")
@click.option("--score", is_flag=True, help="Prefix each line with its probability.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Write every judged line as JSONL: {line_no, text, probability, matched}.",
)
@click.option(
    "--batch-size",
    type=click.IntRange(1, 100),
    default=20,
    show_default=True,
    help="Lines per request. Partial batches go out after 300 ms without input.",
)
@click.option(
    "-j",
    "--jobs",
    type=click.IntRange(1, 16),
    default=4,
    show_default=True,
    help="Maximum requests in flight.",
)
@click.option(
    "--provider",
    type=click.Choice(PROVIDERS),
    default="auto",
    show_default=True,
    help="Where Jev runs. auto: OpenRouter if OPENROUTER_API_KEY is set, else TypeSafe.",
)
@click.option(
    "--model",
    default="jev-latest",
    show_default=True,
    help="Jev model. Pin a version (e.g. jev-1.13) for reproducible results.",
)
@click.option(
    "--explain",
    is_flag=True,
    help="When input ends (or on Ctrl+C), summarize up to 200 selected lines with an LLM.",
)
@click.option(
    "--llm-model",
    default=explaining.DEFAULT_LLM_MODEL,
    show_default=True,
    help="OpenRouter model ID for --explain.",
)
@click.option("--stats", is_flag=True, help="Print usage, cost and latency to stderr.")
@click.version_option(__version__, "-V", "--version", prog_name="jevgrep")
def cli(**options: object) -> None:
    """Print lines of FILES (or stdin) that satisfy QUESTION, judged one by one by TypeSafe's Jev.

    \b
      tail -f server.log | jevgrep "This line reports a real error, not routine noise"
      jevgrep "Mentions a payment failure" app.log --threshold 0.7 --explain

    Exit status: 0 if a line was selected, 1 if none, 2 on error, 130 on Ctrl+C.
    """
    sys.exit(run(**options))  # type: ignore[arg-type]


def run(
    *,
    question: str,
    files: tuple[str, ...],
    threshold: float,
    invert: bool,
    line_number: bool,
    score: bool,
    as_json: bool,
    batch_size: int,
    jobs: int,
    provider: str,
    model: str,
    explain: bool,
    llm_model: str,
    stats: bool,
) -> int:
    if not question.strip():
        raise click.UsageError("QUESTION must not be empty.")
    started = time.perf_counter()
    try:
        jev = resolve_provider(provider)
        explain_key = explaining.explain_api_key() if explain else ""
        judge = make_judge(jev, model)
    except (ConfigError, TypeSafeError) as exc:
        return _fail(exc)

    scanner = Scanner(
        judge, question, threshold=threshold, invert=invert, batch_size=batch_size, jobs=jobs
    )
    sample = explaining.MatchSample() if explain else None
    out = _stdout()
    error: int | None = None
    interrupted = broken_pipe = False
    try:
        for verdict in scanner.scan(read_records(files, sys.stdin)):
            if as_json:
                _write(out, format_json(verdict))
            elif verdict.selected:
                _write(out, format_line(verdict, line_number=line_number, score=score))
            if sample is not None and verdict.selected:
                sample.add(verdict.record)
    except KeyboardInterrupt:
        interrupted = True
    except BrokenPipeError:  # e.g. `| head`: the reader went away, stop quietly
        _silence_stdout()
        broken_pipe = True
    except Exception as exc:
        error = _fail(exc)

    explained = None
    if sample is not None and error is None and not broken_pipe:
        try:
            explained = _explain(question, sample, invert, llm_model, explain_key)
        except KeyboardInterrupt:
            interrupted = True
        except Exception as exc:
            error = _fail(f"--explain failed: {exc}")

    if stats:
        report = format_stats(
            scanner.counts,
            getattr(judge, "usage", None),
            time.perf_counter() - started,
            route=f"{model} via {jev.name}",
            explain=explained.cost if explained else None,
        )
        click.echo(report, err=True)

    if error is not None:
        return error
    if interrupted:
        return EXIT_INTERRUPTED
    return EXIT_MATCH if scanner.counts.selected else EXIT_NO_MATCH


def _explain(
    question: str, sample: explaining.MatchSample, inverted: bool, model: str, api_key: str
) -> explaining.Explanation | None:
    if not sample.total:
        click.echo("jevgrep: nothing to explain, no lines were selected.", err=True)
        return None
    shown = len(sample.records())
    click.echo(f"jevgrep: explaining {shown} selected lines with {model} (Ctrl+C skips)", err=True)
    messages = explaining.build_messages(question, sample, inverted=inverted)
    result = explaining.summarize(messages, model=model, api_key=api_key)
    click.echo(f"\n--- summary ({result.cost.model}, {shown} lines) ---\n{result.text}\n", err=True)
    return result


def _fail(error: BaseException | str) -> int:
    if isinstance(error, TypeSafeAuthenticationError):
        message = f"authentication failed, check your API key ({error})"
    elif isinstance(error, BaseException):
        message = str(error) or type(error).__name__
    else:
        message = error
    click.echo(f"jevgrep: {message}", err=True)
    return EXIT_ERROR


def _stdout() -> TextIO:
    out = sys.stdout
    reconfigure = getattr(out, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")  # never die on a line the terminal can't encode
    return out


def _write(out: TextIO, line: str) -> None:
    out.write(line + "\n")
    out.flush()  # stream matches immediately, even into a pipe


def _silence_stdout() -> None:
    """Point stdout at /dev/null so the interpreter's exit-time flush can't raise again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError):
        pass


def main() -> None:
    cli(prog_name="jevgrep")
