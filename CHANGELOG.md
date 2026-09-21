# Changelog

## 0.1.0 (2026-09-21)

First release, published on PyPI as `jevgrep-cli`.

- Filter any text stream with a yes/no question, one Jev Noul per line.
- Streaming micro-batches: 20 lines, sent after 300 ms idle or 1 s at most, up to 4 requests in
  flight, output kept in input order. Works behind `tail -f`.
- Lines are named in the request state (`lines.line_01`), which removed most answer bleed
  between neighbouring lines on the benchmark (mean F1 0.75 → 0.90).
- grep-style flags and exit codes: `-v`, `-n`, `--score`, `--json`, `--threshold`; 0 / 1 / 2 /
  130. Ctrl+C and closed pipes are handled.
- OpenRouter or TypeSafe, detected from the key that is set; `--model` pins a Jev version.
- `--stats` (provider-reported cost, tokens, p50 latency) and `--explain` (Claude summary via
  OpenRouter).
- Hand-labelled sample log and a benchmark against Claude Haiku 4.5 and Sonnet 5.
