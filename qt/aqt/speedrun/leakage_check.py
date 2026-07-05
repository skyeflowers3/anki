#!/usr/bin/env python3
"""Test-set leakage check for the Speedrun question bank.

Usage:
    out/pyenv/bin/python qt/aqt/speedrun/leakage_check.py

Checks whether any held-out test question is a near-copy of a training or
AI-generated question, which would indicate data leakage and an inflated
performance estimate on the held-out set.

Split logic
-----------
1. If any question in questions.json has ``"held_out": true``, those questions
   form the test set and the remainder form the training set.  This is the
   primary mechanism used in the question bank.
2. Otherwise, the last 20% of questions by list index are held out (same
   proportion as calibrate_performance.py).

Training pool
-------------
• All non-held-out questions from questions.json
• All eval-passed questions from generated_questions.json

Similarity check
----------------
For each test question, every training/generated question is compared using
difflib.SequenceMatcher on three fields:
    • question stem
    • passage (if present on either side)
    • answer choices concatenated

A pair is flagged if the ratio for any field exceeds SIMILARITY_THRESHOLD (0.8).
This catches near-verbatim copies and minor rewordings while ignoring questions
that happen to share a topic or concept.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"
GENERATED_PATH = Path(__file__).resolve().parent / "generated_questions.json"

SIMILARITY_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_questions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("questions", [])
    except Exception as exc:  # noqa: BLE001
        print(f"  Warning: could not load {path.name}: {exc}")
        return []


def _split_questions(
    questions: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Return (test_set, training_set).

    Uses the ``held_out`` field if present on any question; otherwise falls
    back to the last 20% by list index.
    """
    if any(q.get("held_out") for q in questions):
        test = [q for q in questions if q.get("held_out")]
        train = [q for q in questions if not q.get("held_out")]
        return test, train

    cutoff = max(1, int(len(questions) * 0.8))
    return questions[cutoff:], questions[:cutoff]


# ---------------------------------------------------------------------------
# Similarity helpers
# ---------------------------------------------------------------------------

def _ratio(a: str, b: str) -> float:
    """SequenceMatcher similarity ratio, case-insensitive."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


@dataclass
class Match:
    test_id: int
    test_snippet: str
    train_id: int
    train_source: str  # "questions.json" or "generated_questions.json"
    train_snippet: str
    field: str        # which field triggered the match
    ratio: float


def _check_pair(
    test_q: dict,
    train_q: dict,
    train_source: str,
) -> Match | None:
    """Return a Match if any field is above the threshold, else None."""
    checks: list[tuple[str, str, str]] = [
        ("question", test_q.get("question", ""), train_q.get("question", "")),
    ]

    # Only compare passages when both questions have non-empty passage text AND
    # they do not share the same passage_id.  Multiple CARS questions from one
    # passage intentionally share identical passage text — that is not leakage.
    tp = (test_q.get("passage") or "").strip()
    rp = (train_q.get("passage") or "").strip()
    test_pid = test_q.get("passage_id") or ""
    train_pid = train_q.get("passage_id") or ""
    same_passage = test_pid and train_pid and test_pid == train_pid
    if tp and rp and not same_passage:
        checks.append(("passage", tp, rp))

    # Choices: concatenate as a single string for a holistic comparison.
    tc = " | ".join(test_q.get("choices") or [])
    rc = " | ".join(train_q.get("choices") or [])
    if tc and rc:
        checks.append(("choices", tc, rc))

    for fname, a, b in checks:
        if not a or not b:
            continue
        r = _ratio(a, b)
        if r >= SIMILARITY_THRESHOLD:
            return Match(
                test_id=test_q.get("id", -1),
                test_snippet=test_q.get("question", "")[:80],
                train_id=train_q.get("id", -1),
                train_source=train_source,
                train_snippet=train_q.get("question", "")[:80],
                field=fname,
                ratio=r,
            )
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run() -> None:
    print()
    print("=" * 64)
    print("  Speedrun Question-Bank Leakage Check")
    print("=" * 64)

    # Load
    manual_qs = _load_questions(QUESTIONS_PATH)
    generated_qs = _load_questions(GENERATED_PATH)
    eval_passed = [q for q in generated_qs if q.get("eval_passed", False)]

    if not manual_qs:
        sys.exit("  ERROR: questions.json not found or empty.")

    # Split
    test_set, train_set = _split_questions(manual_qs)
    split_method = (
        "held_out field"
        if any(q.get("held_out") for q in manual_qs)
        else "last 20% by index"
    )

    print(f"\n  Split method:          {split_method}")
    print(f"  Test questions:        {len(test_set)}")
    print(f"  Training questions:    {len(train_set)} (manual)")
    print(f"  Generated questions:   {len(eval_passed)} (eval-passed)")
    print(f"  Similarity threshold:  {SIMILARITY_THRESHOLD}")
    print()

    # Check
    matches: list[Match] = []
    for test_q in test_set:
        for train_q in train_set:
            m = _check_pair(test_q, train_q, "questions.json")
            if m:
                matches.append(m)
        for gen_q in eval_passed:
            m = _check_pair(test_q, gen_q, "generated_questions.json")
            if m:
                matches.append(m)

    # Report
    print(f"  Checked {len(test_set)} test question(s) against "
          f"{len(train_set) + len(eval_passed)} training/generated question(s).")
    print()

    if not matches:
        print("  ✓ No near-duplicate pairs found above the threshold.\n")
        print("  STATUS: CLEAN")
    else:
        print(f"  ✗ {len(matches)} flagged pair(s):\n")
        for i, m in enumerate(matches, 1):
            print(f"  [{i}] Test Q#{m.test_id} vs {m.train_source} Q#{m.train_id}")
            print(f"      Field:  {m.field}   Similarity: {m.ratio:.3f}")
            print(f"      Test:   {m.test_snippet!r}...")
            print(f"      Train:  {m.train_snippet!r}...")
            print()
        print("  STATUS: FLAGGED")
        print()
        print("  Recommendation: review flagged pairs and either remove the")
        print("  duplicate from the training pool or move it to the test set.")

    print()


if __name__ == "__main__":
    run()
