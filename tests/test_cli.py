import json

import pytest
from click.testing import CliRunner
from fakes import FakeJudge, keyword_score
from test_judge import FakeClient

from jevgrep import cli
from jevgrep import explain as explaining
from jevgrep.judge import SystemOneJudge
from jevgrep.output import ExplainCost

LOG = "INFO start\nERROR db down\n\nWARN slow\nINFO done\n"


@pytest.fixture
def judge(monkeypatch):
    fake = FakeJudge()
    monkeypatch.setattr(cli, "make_judge", lambda provider, model: fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    return fake


def invoke(*args, input=LOG):
    return CliRunner().invoke(cli.cli, list(args), input=input)


# --- output ------------------------------------------------------------------------------------


def test_prints_lines_at_or_above_the_threshold(judge):
    result = invoke("This line reports a real error")
    assert result.exit_code == 0
    assert result.stdout == "ERROR db down\nWARN slow\n"
    assert judge.questions == ["This line reports a real error"]


def test_threshold_option(judge):
    assert invoke("q", "--threshold", "0.7").stdout == "ERROR db down\n"


def test_invert_prints_lines_below_the_threshold(judge):
    result = invoke("q", "-v")
    assert result.exit_code == 0
    assert result.stdout == "INFO start\nINFO done\n"


def test_line_number_and_score_prefixes(judge):
    assert invoke("q", "-n", "--score").stdout == "2:0.90:ERROR db down\n4:0.50:WARN slow\n"


def test_json_lists_every_judged_line(judge):
    rows = [json.loads(line) for line in invoke("q", "--json").stdout.splitlines()]
    assert rows == [
        {"line_no": 1, "text": "INFO start", "probability": 0.1, "matched": False},
        {"line_no": 2, "text": "ERROR db down", "probability": 0.9, "matched": True},
        {"line_no": 4, "text": "WARN slow", "probability": 0.5, "matched": True},
        {"line_no": 5, "text": "INFO done", "probability": 0.1, "matched": False},
    ]


def test_json_matched_follows_invert(judge):
    rows = [json.loads(line) for line in invoke("q", "--json", "-v").stdout.splitlines()]
    assert [row["matched"] for row in rows] == [True, False, False, True]


def test_several_files_are_prefixed_with_their_names(judge, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.log").write_text("ERROR a\nINFO a\n")
    (tmp_path / "b.log").write_text("ERROR b\n")
    result = invoke("q", "a.log", "b.log", "-n")
    assert result.stdout == "a.log:1:ERROR a\nb.log:1:ERROR b\n"


def test_output_follows_input_order_across_batches(judge):
    lines = [f"ERROR {i}" for i in range(50)]
    result = invoke("q", "--batch-size", "7", "--jobs", "4", input="\n".join(lines) + "\n")
    assert result.stdout.splitlines() == lines


# --- exit codes --------------------------------------------------------------------------------


def test_exit_1_when_nothing_matches(judge):
    result = invoke("q", input="INFO a\nINFO b\n")
    assert (result.exit_code, result.stdout) == (1, "")


def test_exit_2_without_an_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    result = invoke("q")
    assert result.exit_code == 2
    assert "no API key found" in result.stderr
    assert "OPENROUTER_API_KEY" in result.stderr


def test_exit_2_when_the_judge_fails(judge):
    judge.error = RuntimeError("service unavailable")
    result = invoke("q")
    assert result.exit_code == 2
    assert "jevgrep: service unavailable" in result.stderr


@pytest.mark.parametrize(
    "args",
    [["q", "--threshold", "1.5"], ["   "], ["q", "missing.log"], ["q", "--batch-size", "0"]],
)
def test_exit_2_on_usage_errors(judge, args):
    assert invoke(*args).exit_code == 2


def test_provider_flag_selects_the_provider(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "make_judge", lambda p, m: seen.append((p.name, m)) or FakeJudge())
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    invoke("q", "--provider", "typesafe", "--model", "jev-1.13")
    assert seen == [("typesafe", "jev-1.13")]


# --- stats -------------------------------------------------------------------------------------


def test_stats_go_to_stderr(judge):
    result = invoke("q", "--stats")
    assert result.stdout == "ERROR db down\nWARN slow\n"
    assert "lines scanned  5" in result.stderr
    assert "lines matched  2" in result.stderr
    assert "4 (0 from cache, 1 empty)" in result.stderr


def test_stats_report_requests_tokens_cost_and_latency(monkeypatch):
    monkeypatch.setattr(cli, "make_judge", lambda p, m: SystemOneJudge(FakeClient()))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    result = invoke("q", "--stats")
    assert "requests       1" in result.stderr
    assert "input tokens   1,000" in result.stderr
    assert "$0.000042 (estimated at $0.042/Mtok)" in result.stderr
    assert "p50 latency" in result.stderr
    assert "jev-latest via openrouter (answered by jev-1.13.0)" in result.stderr


# --- explain -----------------------------------------------------------------------------------


@pytest.fixture
def summaries(monkeypatch):
    calls = []

    def fake_summarize(messages, *, model, api_key):
        calls.append({"messages": messages, "model": model, "api_key": api_key})
        cost = ExplainCost(model=model, input_tokens=300, output_tokens=60, cost_usd=0.0006)
        return explaining.Explanation("The database is down.", cost)

    monkeypatch.setattr(explaining, "summarize", fake_summarize)
    return calls


def test_explain_summarizes_selected_lines_on_stderr(judge, summaries):
    result = invoke("q", "--explain", "--stats", "--llm-model", "anthropic/claude-sonnet-5")
    assert result.exit_code == 0
    assert result.stdout == "ERROR db down\nWARN slow\n"
    assert "The database is down." in result.stderr
    assert "anthropic/claude-sonnet-5, 300 in / 60 out, $0.000600" in result.stderr

    [call] = summaries
    assert (call["model"], call["api_key"]) == ("anthropic/claude-sonnet-5", "sk-test")
    user = call["messages"][1]["content"]
    assert "ERROR db down" in user and "WARN slow" in user and "INFO start" not in user


def test_explain_runs_on_ctrl_c_with_the_lines_seen_so_far(judge, summaries):
    def score(line):
        if line == "STOP":
            raise KeyboardInterrupt  # what Ctrl+C looks like mid-scan
        return keyword_score(line)

    judge.score = score
    result = invoke(
        "q", "--explain", "--batch-size", "2", "--jobs", "1", input="ERROR a\nINFO b\nSTOP\n"
    )
    assert result.exit_code == 130
    assert result.stdout == "ERROR a\n"
    [call] = summaries
    assert "ERROR a" in call["messages"][1]["content"]


def test_explain_skips_the_llm_when_nothing_matched(judge, summaries):
    result = invoke("q", "--explain", input="INFO a\n")
    assert result.exit_code == 1
    assert summaries == []
    assert "nothing to explain" in result.stderr


def test_explain_needs_openrouter_even_with_a_typesafe_key(monkeypatch, summaries):
    monkeypatch.setattr(cli, "make_judge", lambda p, m: FakeJudge())
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-test")
    assert invoke("q").exit_code == 0
    result = invoke("q", "--explain")
    assert result.exit_code == 2
    assert "--explain needs OPENROUTER_API_KEY" in result.stderr
