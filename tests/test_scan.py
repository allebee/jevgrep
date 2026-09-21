import io
import os
import threading
import time

import pytest
from fakes import FakeJudge

from jevgrep.judge import JudgeError
from jevgrep.scan import Record, Scanner, read_records


def records(lines):
    return [Record(i, text) for i, text in enumerate(lines, start=1)]


def run_scan(scanner, source, timeout=10.0):
    """Collect every verdict, failing (instead of hanging) if the scan never finishes."""
    verdicts, errors = [], []

    def consume():
        try:
            verdicts.extend(scanner.scan(source))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "scan did not finish"
    if errors:
        raise errors[0]
    return verdicts


# --- batching ----------------------------------------------------------------------------------


def test_sends_full_batches_then_the_remainder():
    judge = FakeJudge()
    lines = [f"line {i}" for i in range(45)]
    verdicts = run_scan(Scanner(judge, "q", batch_size=20), records(lines))
    assert [len(call) for call in judge.calls] == [20, 20, 5]
    assert [v.record.text for v in verdicts] == lines


def test_partial_batch_is_sent_after_idle_input():
    judge = FakeJudge()
    stream_open = threading.Event()

    def source():  # two lines, then the input stays open like `tail -f`
        yield Record(1, "ERROR one")
        yield Record(2, "two")
        stream_open.wait(5)

    verdicts = Scanner(judge, "q", batch_size=20, idle=0.05, max_wait=30).scan(source())
    started = time.monotonic()
    first = [next(verdicts), next(verdicts)]
    elapsed = time.monotonic() - started
    stream_open.set()

    assert [v.record.line_no for v in first] == [1, 2]
    assert judge.calls == [["ERROR one", "two"]]
    assert elapsed < 1.0


def test_slow_trickle_is_flushed_by_max_wait():
    judge = FakeJudge()

    def source():  # a line every 30 ms never looks idle
        for i in range(12):
            yield Record(i + 1, f"line {i}")
            time.sleep(0.03)

    run_scan(Scanner(judge, "q", batch_size=100, idle=0.5, max_wait=0.1), source())
    assert len(judge.calls) >= 2


# --- ordering and concurrency ------------------------------------------------------------------


def test_output_keeps_input_order_when_a_later_batch_finishes_first():
    judge = FakeJudge(delay=lambda lines: 0.3 if "line 0" in lines else 0.0)
    lines = [f"line {i}" for i in range(20)]
    verdicts = run_scan(Scanner(judge, "q", batch_size=5, jobs=4), records(lines))
    assert [v.record.text for v in verdicts] == lines
    assert len(judge.calls) == 4


def test_batches_run_concurrently_up_to_jobs():
    judge = FakeJudge(delay=0.25)
    started = time.monotonic()
    run_scan(Scanner(judge, "q", batch_size=5, jobs=4), records(f"l{i}" for i in range(20)))
    assert time.monotonic() - started < 0.75  # four sequential batches would take 1 s


# --- caching, empty and long lines -------------------------------------------------------------


def test_identical_lines_are_judged_once():
    judge = FakeJudge(delay=0.05)
    lines = ["ERROR disk full", "ok", "ERROR disk full", "ok"] * 5
    scanner = Scanner(judge, "q", batch_size=4, jobs=4)
    verdicts = run_scan(scanner, records(lines))

    assert sorted(judge.judged) == ["ERROR disk full", "ok"]
    assert [v.probability for v in verdicts] == [0.9, 0.1] * 10
    assert (scanner.counts.judged, scanner.counts.cached) == (2, 18)


def test_cache_is_bounded():
    judge = FakeJudge()
    lines = ["a", "b", "c", "a"]
    run_scan(Scanner(judge, "q", batch_size=1, jobs=1, cache_size=2), records(lines))
    assert judge.judged == ["a", "b", "c", "a"]  # "a" was evicted before it came back


def test_empty_lines_are_skipped():
    judge = FakeJudge()
    scanner = Scanner(judge, "q")
    verdicts = run_scan(scanner, records(["", "   ", "ERROR x", "\t"]))
    assert [v.record.line_no for v in verdicts] == [3]
    assert judge.judged == ["ERROR x"]
    assert (scanner.counts.read, scanner.counts.empty) == (4, 3)


def test_long_lines_are_truncated_for_the_judge_only():
    line = "ERROR " + "x" * 1000
    judge = FakeJudge()
    verdicts = run_scan(Scanner(judge, "q"), records([line]))
    assert judge.judged == [line[:500]]
    assert verdicts[0].record.text == line


# --- threshold and invert ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("threshold", "invert", "expected"),
    [
        (0.5, False, [True, True, False]),  # 0.9 and 0.5 are at/above 0.5
        (0.6, False, [True, False, False]),
        (0.5, True, [False, False, True]),
        (0.0, False, [True, True, True]),
    ],
)
def test_threshold_and_invert(threshold, invert, expected):
    scanner = Scanner(FakeJudge(), "q", threshold=threshold, invert=invert)
    verdicts = run_scan(scanner, records(["ERROR a", "WARN b", "INFO c"]))
    assert [v.selected for v in verdicts] == expected
    assert scanner.counts.selected == sum(expected)


# --- failures ----------------------------------------------------------------------------------


def test_judge_errors_reach_the_caller():
    judge = FakeJudge(error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        run_scan(Scanner(judge, "q"), records(["x"]))


def test_wrong_number_of_answers_is_an_error():
    class ShortJudge:
        def judge(self, lines, question):
            return []

    with pytest.raises(JudgeError):
        run_scan(Scanner(ShortJudge(), "q"), records(["x"]))


def test_read_errors_reach_the_caller():
    def source():
        yield Record(1, "x")
        raise OSError("disk went away")

    with pytest.raises(OSError, match="disk went away"):
        run_scan(Scanner(FakeJudge(), "q"), source())


# --- input -------------------------------------------------------------------------------------


def test_read_records_labels_sources_only_for_several_inputs(tmp_path):
    a = tmp_path / "a.log"
    a.write_text("one\ntwo\r\n")
    b = tmp_path / "b.log"
    b.write_text("three")

    assert list(read_records([str(a)], io.StringIO())) == [Record(1, "one"), Record(2, "two")]
    several = read_records([str(a), "-", str(b)], io.StringIO("in\n"))
    assert [(r.source, r.line_no, r.text) for r in several] == [
        (str(a), 1, "one"),
        (str(a), 2, "two"),
        ("(standard input)", 1, "in"),
        (str(b), 1, "three"),
    ]


def test_stdin_is_read_from_its_file_descriptor():
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"caf\xc3")  # a multi-byte character split across reads
    os.write(write_fd, b"\xa9\r\nbad \xff byte\nlast")
    os.close(write_fd)
    with os.fdopen(read_fd) as stdin:
        texts = [r.text for r in read_records([], stdin)]
    assert texts == ["café", "bad � byte", "last"]
