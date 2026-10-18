# token-speed-check

An OpenCode / T3 Code skill for measuring and explaining LLM tokens-per-second (tok/s) throughput.

It answers questions like:

- "Check the tokens per second the LLM was running on."
- "How fast was that session?"
- "Was the model slow, or was it waiting on tools?"
- "Compare throughput between these two turns."

## What it reports

For each T3 run or OpenCode session segment:

- **Generation throughput** — tokens generated while the model was actively streaming. This is the model's decode speed.
- **End-to-end throughput** — generated tokens divided by total wall time, including tool calls, approvals, retries, and idle gaps.
- **Text-only throughput** — excludes reasoning tokens.
- **Per-message distribution** — p50, p75, p90, and max tok/s.
- Token counts — output, reasoning, input, and cached input.

The key distinction is that end-to-end speed is usually much lower than generation speed because tool execution dominates wall time.

## Install

Copy this directory into your OpenCode skills folder:

```bash
cp -r token-speed-check ~/.config/opencode/skills/
```

OpenCode discovers `SKILL.md` automatically. No dependencies beyond Python 3 and the standard library.

## Usage

```bash
python3 scripts/token_speed.py <thread-id-or-session-id>
python3 scripts/token_speed.py <thread-id-or-session-id> --json
```

Examples:

```bash
# T3 Code thread UUID
python3 scripts/token_speed.py 56de57b4-276a-4fbf-b31a-6383099883e2

# OpenCode session ID
python3 scripts/token_speed.py ses_eb111e836ffe8gKJUhsnq1J0oD
```

Default data locations:

| Store | Path |
|---|---|
| T3 Code state | `~/.t3/userdata/statev2.sqlite` |
| OpenCode messages | `~/.local/share/opencode/opencode.db` |

Override with `--state-db` and `--opencode-db`. The script opens both databases read-only, so it is safe to run while the apps are active.

## Important behavior

A single OpenCode session can serve multiple T3 runs. The script resolves each T3 run to its native session and filters assistant messages by the run window. This avoids blending turns together and producing a plausible but wrong tok/s.

When given only an OpenCode session ID, it splits messages into segments at gaps longer than five minutes (`--gap-seconds`).

## Example output

```text
Thread: 56de57b4-276a-4fbf-b31a-6383099883e2 - Revamp Homepage Header and Hero
Model: openrouter/deepseek/deepseek-v4.1-flash-1017

Run 1 (completed)  2026-10-18 12:13:09 -> 2026-10-18 12:26:32 UTC  (803.4s)
  Messages: 118 assistant messages
  Generated: 48,015 tokens (15,435 output + 32,580 reasoning)
  Input: 9,564,243 tokens (9,446,400 cached)
  Generation: 113.8 tok/s over 421.9s of streaming
  End-to-end: 59.8 tok/s over 803.4s wall time
  Text-only: 36.6 tok/s
  Per-message: p50 76.6 | p75 105.4 | p90 146.9 | max 226.7 tok/s
```

## Files

```text
token-speed-check/
├── SKILL.md
├── scripts/token_speed.py
└── evals/evals.json
```

## Tests

```bash
python3 -m py_compile scripts/token_speed.py
python3 scripts/token_speed.py ses_eb111e836ffe8gKJUhsnq1J0oD --json
```
