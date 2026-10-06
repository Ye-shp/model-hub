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

## Decisions the hub hands to Jev

Qwen rarely calls optional tools, so the controller asks Jev directly at fixed points (`agents/cowork/judge.py`). Each
one falls back to the hub's earlier behaviour when Jev isn't connected, is over its daily limit, fails or takes longer
than `COWORK_JEV_TIMEOUT` seconds (default 25). `COWORK_JEV_DECISIONS=off` turns them all off; `ask_jev` stays.

| Where | Question | Effect |
| --- | --- | --- |
| Before a new Cowork request | Would asking the user first change the result? | Below 0.35: no questions and no Qwen call; otherwise Qwen writes up to 3 questions |
| Before any owner task | Does the message ask Claude to do the work? Is it mainly substantial software work? | Asked: Claude Code gets the request first. Coding (0.85+): the same, with a coding brief (`COWORK_AUTO_CLAUDE=off` disables this part) |
| After the Cowork reply | How completely do the reply and files deliver the request? Does it defer its own work? | Short or deferring: one fix round in the same task, never more |
| After a time or step limit | Is the work finished, or blocked on the user? | 0.8+: no automatic continuation |
| `study_link` | Does the post contain reusable tactics? | Below 0.15: a short summary instead of the full extraction |
| `trend_research` | Relevance of each shortlisted result (one score per result, 40 per request) | Replaces the engine's keyword fallback ranking (`vendor/last30days/scripts/lib/jev_rerank.py`) |
| `publish_post`, phone taps on post/send/like buttons and Enter | Does the message clearly approve this now (not negated, conditional or a question)? | Can only withdraw an approval the regex found, never grant one |

Each request is logged as a `jev:` hand-off and counts toward `JEV_DAILY_REQUESTS`.

## Claude Code

Before the first Claude Code hand-off, the box runs, as the owner's sandbox user:

    claude plugin marketplace add typesafe-ai/skills
    claude plugin install typesafe@typesafe-ai

(`escalate.CLAUDE_PLUGINS`; done once, retried on the next hand-off if it failed). Claude Code also receives the key
as `TYPESAFE_API_KEY` to test what it builds.

Settings: `TYPESAFE_MODEL` (default `jev-latest`), `TYPESAFE_URL`, `JEV_DAILY_REQUESTS`.
