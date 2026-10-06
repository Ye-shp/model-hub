# Jev (TypeSafe) in Cowork

Jev is TypeSafe's System One model: it answers typed questions about some state (yes/no probability, one option of
a set, or a position on 2-10 levels) with probabilities. Cowork uses it for classifying, routing, ranking, scoring
and checking items, and decides on its own when that fits (`agents/jev.py`, guidance in `jev.GUIDE`).

## Connect

In an owner Cowork chat: `/connect typesafe <API key>` (the key starts with `apikey_`). `/connections` shows
"Jev (TypeSafe)" with today's request count; `/connect typesafe off` removes the key. The key is stored root-only in
`DATA/typesafe-key` (or set `TYPESAFE_API_KEY` on the instance) and is never shown to the model.

## What Cowork gets

- `ask_jev(state_json, questions_json)`: for the owner's tasks that may use paid frontier models (the same switch as
  Claude Code), lead agent and helpers. Each request is logged as a hand-off (`jev:` in the console's escalations) and
  counted against `JEV_DAILY_REQUESTS` (default 2000 per 24 hours). A failed request doesn't stop the task.
- When a request is about building software with TypeSafe, the `skills/typesafe` playbook is added to the prompt and
  the coding goes to Claude Code.

## Claude Code

Before the first Claude Code hand-off, the box runs, as the owner's sandbox user:

    claude plugin marketplace add typesafe-ai/skills
    claude plugin install typesafe@typesafe-ai

(`escalate.CLAUDE_PLUGINS`; done once, retried on the next hand-off if it failed). Claude Code also receives the key
as `TYPESAFE_API_KEY` to test what it builds.

Settings: `TYPESAFE_MODEL` (default `jev-latest`), `TYPESAFE_URL`, `JEV_DAILY_REQUESTS`.
