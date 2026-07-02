#!/usr/bin/env python3
"""Eval pipeline for Speedrun's AI question generator.

Scores each generated question 0.0–1.0 on four criteria using GPT-4o as
judge.  The gold set (speedrun/questions.json) is shown to the judge as the
reference standard — the criteria are inherently comparative to it:

  difficulty_match    — difficulty matches the gold MCAT questions
  style_match         — passage format and style matches the gold MCAT questions
  answer_defensible   — the correct answer is clearly and unambiguously correct
  distractors_quality — wrong answers are plausibly wrong but not obviously so

A score of 1.0 means indistinguishable from a real AAMC question on that
criterion.  Pass threshold: 0.75 (average across all four criteria).

Usage (standalone)
------------------
    python speedrun/eval.py                          # score all unevaluated
    python speedrun/eval.py --passage-id bio_gen_001 # specific batch
    python speedrun/eval.py --all --verbose          # re-score with details

Library usage
-------------
    from speedrun.eval import run_eval
    annotated, summary = run_eval(questions, client)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

QUESTIONS_FILE = Path(__file__).parent / "questions.json"
GENERATED_FILE = Path(__file__).parent / "generated_questions.json"
RESULTS_FILE = Path(__file__).parent / "eval_results.json"

CUTOFF = 0.75
CRITERIA = [
    "difficulty_match",
    "style_match",
    "answer_defensible",
    "distractors_quality",
]

_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_MAX_RETRIES = 6

# ---------------------------------------------------------------------------
# Judge prompt
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = """\
You are an expert MCAT question evaluator with deep knowledge of the AAMC exam
format and medical school prerequisites.

You will be shown real AAMC gold-standard questions as the reference, then asked
to score an AI-generated question against that gold set on four criteria
(0.0–1.0 each, two decimal places).

Criteria:
  difficulty_match    — How well does the difficulty match the gold MCAT questions?
                        (0 = far too easy or hard relative to gold, 1 = indistinguishable)
  style_match         — How well does the passage-based format and question style
                        match the gold MCAT questions?
                        (0 = clearly not MCAT-style, 1 = indistinguishable from gold)
  answer_defensible   — Is the marked correct answer clearly and unambiguously
                        correct? (0 = indefensible or debatable, 1 = clearly correct)
  distractors_quality — Are the wrong answers plausibly wrong but not obviously so?
                        (0 = trivial or flawed distractors, 1 = excellent, like gold)

Return ONLY a JSON object — no markdown, no prose — with this exact schema:
{
  "difficulty_match": <float 0.0-1.0>,
  "style_match": <float 0.0-1.0>,
  "answer_defensible": <float 0.0-1.0>,
  "distractors_quality": <float 0.0-1.0>,
  "reasoning": "<one concise sentence explaining the scores>"
}"""


def _judge_user_prompt(question: dict, gold_examples: list[dict]) -> str:
    gold_block = json.dumps(
        [
            {
                "question": g.get("question", ""),
                "choices": g.get("choices", []),
                "correct_answer": g.get("correct_answer", ""),
                "concept": g.get("concept", ""),
            }
            for g in gold_examples
        ],
        indent=2,
        ensure_ascii=False,
    )
    q_block = json.dumps(
        {
            "passage": question.get("passage", ""),
            "question": question.get("question", ""),
            "choices": question.get("choices", []),
            "correct_answer": question.get("correct_answer", ""),
            "concept": question.get("concept", ""),
            "rationale": question.get("rationale", ""),
        },
        indent=2,
        ensure_ascii=False,
    )
    return (
        f"GOLD STANDARD MCAT QUESTIONS (topic: {question.get('topic', 'unknown')}):\n"
        f"{gold_block}\n\n"
        f"AI-GENERATED QUESTION TO EVALUATE:\n{q_block}"
    )


# ---------------------------------------------------------------------------
# OpenAI helper
# ---------------------------------------------------------------------------


def _chat_with_backoff(client: Any, messages: list[dict], model: str = "gpt-4o") -> str:
    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,  # low temp for consistent scoring
            )
            return response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            is_rate_limit = "rate_limit" in err.lower() or "429" in err
            is_last = attempt == _MAX_RETRIES - 1
            if is_last or not is_rate_limit:
                raise
            wait = min(backoff * (2**attempt), _MAX_BACKOFF)
            print(
                f"  Rate limit (attempt {attempt + 1}/{_MAX_RETRIES}), "
                f"retrying in {wait:.0f}s…",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")


def _extract_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_question(
    client: Any,
    question: dict,
    gold_examples: list[dict],
    model: str = "gpt-4o",
) -> dict:
    """Score one question via GPT-4o judge against gold_examples.

    Returns per-criterion scores, average, and reasoning.
    On API error returns scores of 0.0 with eval_error=True.
    """
    try:
        raw = _chat_with_backoff(
            client,
            [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": _judge_user_prompt(question, gold_examples),
                },
            ],
            model=model,
        )
        data = json.loads(_extract_json(raw))
    except Exception as exc:  # noqa: BLE001
        return {
            "difficulty_match": 0.0,
            "style_match": 0.0,
            "answer_defensible": 0.0,
            "distractors_quality": 0.0,
            "average": 0.0,
            "reasoning": f"[eval error: {exc}]",
            "eval_error": True,
        }

    scores = {c: float(data.get(c, 0.0)) for c in CRITERIA}
    scores["average"] = round(sum(scores.values()) / len(CRITERIA), 4)
    scores["reasoning"] = data.get("reasoning", "")
    return scores


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def _load_all_questions() -> list[dict]:
    if not QUESTIONS_FILE.exists():
        return []
    return json.loads(QUESTIONS_FILE.read_text(encoding="utf-8")).get("questions", [])


def load_held_out(topic: str | None = None) -> tuple[list[dict], bool]:
    """Return (held-out questions for eval, used_fallback).

    Prefers held-out questions matching *topic*.  If none match, falls back to
    all held-out questions and returns used_fallback=True.
    """
    all_qs = _load_all_questions()
    held = [q for q in all_qs if q.get("held_out") is True]
    if not held:
        # No held-out set defined — fall back to all questions
        return all_qs, True
    if topic:
        same_topic = [q for q in held if q.get("topic") == topic]
        if same_topic:
            return same_topic, False
        # Topic not represented in held-out set — use all held-out
        return held, True
    return held, False


def load_style_examples(topic: str | None = None, n: int = 5) -> list[dict]:
    """Return up to *n* non-held-out questions as style examples for the generator.

    Prefers questions matching *topic*, fills from any topic if needed.
    """
    all_qs = _load_all_questions()
    training = [q for q in all_qs if not q.get("held_out", False)]
    if not training:
        training = all_qs  # no held_out field yet — use everything
    if topic:
        same = [q for q in training if q.get("topic") == topic]
        pool = (same + [q for q in training if q not in same])[:n]
    else:
        pool = training[:n]
    return pool[:n]


def load_generated() -> list[dict]:
    if not GENERATED_FILE.exists():
        return []
    data = json.loads(GENERATED_FILE.read_text(encoding="utf-8"))
    return data.get("questions", [])


def save_generated(questions: list[dict]) -> None:
    GENERATED_FILE.write_text(
        json.dumps({"questions": questions}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Batch eval
# ---------------------------------------------------------------------------


def eval_batch(
    client: Any,
    questions: list[dict],
    *,
    model: str = "gpt-4o",
    verbose: bool = False,
) -> tuple[list[dict], dict]:
    """Score *questions* and return (annotated_questions, summary).

    Each annotated question gains:
        eval_score           float — average of four criteria (0.0–1.0)
        eval_passed          bool  — True if score >= CUTOFF
        eval_criteria        dict  — per-criterion scores
        eval_reasoning       str   — one-sentence explanation
        eval_held_out_ids    list  — IDs of held-out questions used as reference
        eval_topic_fallback  bool  — True if no same-topic held-out question existed
    """
    if not questions:
        return [], _empty_summary()

    scores_all: list[float] = []
    failed_ids: list[str] = []
    annotated: list[dict] = []

    # Coverage tracking: topic → "matched" | "fallback"
    topic_coverage: dict[str, str] = {}

    for i, q in enumerate(questions, 1):
        topic = q.get("topic")
        held_examples, used_fallback = load_held_out(topic)
        ref_ids = [str(h.get("id", "?")) for h in held_examples[:5]]

        if topic:
            if topic not in topic_coverage:
                topic_coverage[topic] = "fallback" if used_fallback else "matched"

        if verbose:
            fallback_note = (
                " [fallback — no same-topic held-out]" if used_fallback else ""
            )
            print(
                f"  [{i}/{len(questions)}] {q.get('id', '?')} "
                f"(topic={topic}{fallback_note}) …",
                end=" ",
                flush=True,
            )

        result = score_question(client, q, held_examples[:5], model=model)
        avg = result["average"]
        scores_all.append(avg)
        passed = not result.get("eval_error", False) and avg >= CUTOFF
        if not passed:
            failed_ids.append(str(q.get("id", "?")))

        annotated_q = dict(q)
        annotated_q["eval_score"] = avg
        annotated_q["eval_passed"] = passed
        annotated_q["eval_criteria"] = {c: result[c] for c in CRITERIA}
        annotated_q["eval_reasoning"] = result.get("reasoning", "")
        annotated_q["eval_held_out_ids"] = ref_ids
        annotated_q["eval_topic_fallback"] = used_fallback
        annotated.append(annotated_q)

        if verbose:
            status = "PASS" if passed else "FAIL"
            print(f"{status} ({avg:.2f}) — {result.get('reasoning', '')}")

    n = len(questions)
    n_passed = sum(1 for q in annotated if q["eval_passed"])
    avg_score = round(sum(scores_all) / n, 4) if scores_all else 0.0
    wrong_rate = round(len(failed_ids) / n, 4) if n else 0.0
    n_fallback = sum(1 for q in annotated if q.get("eval_topic_fallback"))

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cutoff": CUTOFF,
        "questions_evaluated": n,
        "questions_passed": n_passed,
        "questions_failed": n - n_passed,
        "pass_rate": round(n_passed / n, 4) if n else 0.0,
        "wrong_answer_rate": wrong_rate,
        "avg_score": avg_score,
        "failed_question_ids": failed_ids,
        "held_out_coverage": topic_coverage,
        "fallback_count": n_fallback,
    }
    return annotated, summary


def _empty_summary() -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cutoff": CUTOFF,
        "questions_evaluated": 0,
        "questions_passed": 0,
        "questions_failed": 0,
        "pass_rate": 0.0,
        "wrong_answer_rate": 0.0,
        "avg_score": 0.0,
        "failed_question_ids": [],
        "held_out_coverage": {},
        "fallback_count": 0,
    }


# ---------------------------------------------------------------------------
# Results persistence
# ---------------------------------------------------------------------------


def save_results(summary: dict, passage_id: str | None = None) -> None:
    existing: list[dict] = []
    if RESULTS_FILE.exists():
        try:
            existing = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = [existing]
        except Exception:
            existing = []
    entry = dict(summary)
    if passage_id:
        entry["passage_id"] = passage_id
    existing.append(entry)
    RESULTS_FILE.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Public entry point (library)
# ---------------------------------------------------------------------------


def run_eval(
    questions: list[dict],
    client: Any,
    *,
    model: str = "gpt-4o",
    verbose: bool = False,
) -> tuple[list[dict], dict]:
    """Evaluate *questions* and return (annotated_questions, summary)."""
    print(f"\nRunning eval on {len(questions)} question(s)…")
    return eval_batch(client, questions, model=model, verbose=verbose)


def print_summary(summary: dict) -> None:
    n = summary["questions_evaluated"]
    passed = summary["questions_passed"]
    failed = summary["questions_failed"]
    avg = summary["avg_score"]
    wrong_rate = summary["wrong_answer_rate"]
    coverage = summary.get("held_out_coverage", {})
    fallback_count = summary.get("fallback_count", 0)

    print("\n" + "=" * 56)
    print("  EVAL RESULTS")
    print("=" * 56)
    print(f"  Evaluated        : {n}")
    print(f"  Passed (≥{CUTOFF})  : {passed}  ({100 * passed // n if n else 0}%)")
    print(f"  Failed           : {failed}")
    print(f"  Avg score        : {avg:.3f}  (1.0 = indistinguishable from gold)")
    print(f"  Wrong-answer rate: {wrong_rate:.0%}")

    if coverage:
        print("\n  Held-out topic coverage:")
        for topic, status in sorted(coverage.items()):
            marker = "✓" if status == "matched" else "⚠ fallback"
            print(f"    {marker}  {topic}")
    if fallback_count:
        print(
            f"\n  ⚠  {fallback_count} question(s) used fallback held-out set "
            f"(no same-topic held-out question exists)."
        )

    if summary["failed_question_ids"]:
        ids = ", ".join(summary["failed_question_ids"])
        print(f"\n  Failed IDs : {ids}")
    print("=" * 56)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Eval pipeline for Speedrun AI question generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--passage-id", metavar="ID", help="Only evaluate this passage_id."
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Re-evaluate all (including already-evaluated).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Print per-question details."
    )
    parser.add_argument(
        "--model", default="gpt-4o", help="Judge model (default: gpt-4o)."
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from speedrun.question_generator import _openai_client  # type: ignore[import]

        client = _openai_client()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"ERROR: Could not initialise OpenAI client: {exc}", file=sys.stderr)
        sys.exit(1)

    all_generated = load_generated()
    if not all_generated:
        print("No generated questions found in generated_questions.json.")
        return

    if args.passage_id:
        to_eval = [q for q in all_generated if q.get("passage_id") == args.passage_id]
        if not to_eval:
            print(f"No questions found with passage_id={args.passage_id!r}")
            sys.exit(1)
    elif args.all:
        to_eval = all_generated
    else:
        to_eval = [q for q in all_generated if "eval_score" not in q]
        if not to_eval:
            print("All questions already evaluated. Use --all to re-evaluate.")
            return

    annotated, summary = eval_batch(
        client, to_eval, model=args.model, verbose=args.verbose
    )

    annotated_by_id = {q["id"]: q for q in annotated}
    merged = [annotated_by_id.get(q["id"], q) for q in all_generated]
    save_generated(merged)

    passage_id = to_eval[0].get("passage_id") if to_eval else None
    save_results(summary, passage_id=passage_id)

    print_summary(summary)
    print(f"\n  Results saved to : {RESULTS_FILE}")
    print(f"  Scores written   : {GENERATED_FILE}")


if __name__ == "__main__":
    main()
