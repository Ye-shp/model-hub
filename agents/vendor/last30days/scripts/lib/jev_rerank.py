"""Model Hub addition (not upstream): relevance reranking with Jev, TypeSafe's typed-judgment model.

When no reasoning provider is configured but TYPESAFE_API_KEY is set (Model Hub passes it for the owner's tasks),
each shortlisted candidate gets one score question about its relevance to the topic. Up to 40 questions go in one
request; they are answered in parallel and can't see one another. The result has the same shape as the LLM
reranker's JSON, so rerank._apply_llm_scores handles it unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from . import http, schema

URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
BATCH = 40
LEVELS = [
    "Off-target: not about the topic, or doesn't mention its main subject",
    "Weak: only touches the topic, or redundant filler",
    "Somewhat relevant, but thin evidence",
    "Clearly relevant and useful evidence",
    "One of the strongest pieces of evidence on the topic",
]


def available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def _state(topic: str, plan: schema.QueryPlan, batch: list[schema.Candidate], primary_entity: str) -> dict[str, Any]:
    return {
        "topic": topic,
        "intent": plan.intent,
        "primary_entity": primary_entity or None,
        "what_the_user_wants_to_know": [subquery.ranking_query for subquery in plan.subqueries],
        "candidates": {
            f"c{n}": {
                "source": schema.candidate_source_label(candidate),
                "title": candidate.title[:220],
                "snippet": candidate.snippet[:420],
                "date": schema.candidate_best_published_at(candidate) or "unknown",
            }
            for n, candidate in enumerate(batch)
        },
    }


def score(topic: str, plan: schema.QueryPlan, candidates: list[schema.Candidate], primary_entity: str = "",
          intent_hint: str = "") -> dict[str, Any]:
    """{"scores": [{"candidate_id", "relevance" (0-100), "reason"}]}, like the LLM reranker returns."""
    key = os.environ["TYPESAFE_API_KEY"]
    rows = []
    for start in range(0, len(candidates), BATCH):
        batch = candidates[start:start + BATCH]
        entity = (f" A candidate that doesn't mention \"{primary_entity}\" (or a clear synonym) is at most 'Weak'."
                  if primary_entity else "")
        questions = {
            f"c{n}": {
                "type": "score",
                "instructions": f"How strong is candidate c{n} as evidence for what the user wants to know about the "
                                f"topic?{entity} {intent_hint}".strip(),
                "criteria": LEVELS,
            }
            for n in range(len(batch))
        }
        response = http.post(URL, {"model": MODEL, "state": _state(topic, plan, batch, primary_entity),
                                   "questions": questions},
                             headers={"Authorization": f"Bearer {key}"}, timeout=60, retries=2)
        answers = response.get("answers") or {}
        for n, candidate in enumerate(batch):
            answer = answers.get(f"c{n}") or {}
            if "score" not in answer:
                continue
            relevance = float(answer["score"]) / (len(LEVELS) - 1) * 100.0
            rows.append({"candidate_id": candidate.candidate_id, "relevance": round(relevance, 1),
                         "reason": f"jev relevance {float(answer['score']):.2f}/4 (confidence "
                                   f"{float(answer.get('confidence') or 0):.2f})"})
    return {"scores": rows}
