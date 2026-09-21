#!/usr/bin/env bash
# Plays the README demo against the live API. To re-record docs/demo.gif (asciinema 3 + agg):
#   OPENROUTER_API_KEY=... asciinema rec --headless --window-size 140x32 -c docs/demo.sh demo.cast
#   agg --font-size 15 demo.cast docs/demo.gif
set -u
repo=$(cd "$(dirname "$0")/.." && pwd)
export PATH="$repo/.venv/bin:$PATH"
cd "$(mktemp -d)"
cp "$repo/examples/sample.log" app.log
: >server.log

say() {  # show a prompt and "type" a command
  printf '\033[1;32m$\033[0m '
  for ((i = 0; i < ${#1}; i++)); do printf '%s' "${1:i:1}"; sleep 0.025; done
  printf '\n'
  sleep 0.5
}

# 1. Follow a live log. A background writer replays the incident from the sample, a line
#    every 0.3 s; only the real problems come out the other end.
awk 'NR >= 56 && NR <= 108 && length($0) < 140' app.log |
  while IFS= read -r line; do printf '%s\n' "$line"; sleep 0.3; done >>server.log &
say 'tail -f server.log | jevgrep "This line reports a real error, not routine noise"'
tail -f server.log | jevgrep "This line reports a real error, not routine noise" --model jev-1.13 &
disown  # no "Terminated" notice when we stop it below
sleep 15
pkill -TERM -f "jevgrep This line reports"
pkill -TERM -f "tail -f server.log"
sleep 0.3
printf '^C\n'
sleep 1

# 2. Ask Claude what the matches have in common (the summary goes to stderr).
say 'jevgrep "Mentions a payment failure" app.log --explain > /dev/null'
jevgrep "Mentions a payment failure" app.log --explain --model jev-1.13 >/dev/null
sleep 4
