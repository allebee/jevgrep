"""End-to-end checks in a real process: pipes, signals and streaming. Still no network."""

import os
import signal
import subprocess
import sys
import time

SCRIPT = """
from jevgrep import cli

class KeywordJudge:
    def judge(self, lines, question):
        return [0.9 if "ERROR" in line else 0.1 for line in lines]

cli.make_judge = lambda provider, model: KeywordJudge()
cli.main()
"""


def jevgrep(*args, **popen_args):
    env = {**os.environ, "OPENROUTER_API_KEY": "sk-test"}
    return subprocess.Popen([sys.executable, "-c", SCRIPT, *args], env=env, **popen_args)


def test_closed_output_pipe_exits_quietly(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("ERROR boom\n" * 20_000)  # far more output than a pipe buffer holds
    proc = jevgrep("errors", str(log), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    first = proc.stdout.readline()
    proc.stdout.close()  # like `| head -1`
    stderr = proc.stderr.read()
    proc.wait(timeout=20)
    assert first == b"ERROR boom\n"
    assert proc.returncode == 0
    assert stderr == b""


def test_matches_stream_while_input_is_open_and_ctrl_c_exits_130():
    proc = jevgrep(
        "errors", "--stats",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip
    try:
        proc.stdin.write(b"INFO warm-up\nERROR first\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == b"ERROR first\n"  # includes interpreter start-up

        started = time.monotonic()
        proc.stdin.write(b"ERROR second\n")
        proc.stdin.flush()
        assert proc.stdout.readline() == b"ERROR second\n"
        assert time.monotonic() - started < 1.0  # flushed by the 300 ms idle timer

        proc.send_signal(signal.SIGINT)
        stderr = proc.stderr.read()
        proc.wait(timeout=10)
    finally:
        proc.kill()
    assert proc.returncode == 130
    assert b"lines scanned  3" in stderr
    assert b"Traceback" not in stderr
    assert b"Fatal Python error" not in stderr
