"""Background AI question generation for the Speedrun adaptive loop.

Triggered automatically when a question block is about to be served for a topic
that has fewer than ``GENERATION_THRESHOLD`` approved questions.  Generates
exactly enough questions to bring the pool back up to GENERATION_THRESHOLD
(with a small overage to buffer against eval failures).  Runs entirely in a
daemon thread — the student session is never blocked.

Errors at every stage are logged and silently swallowed; the app will never
crash because of this module.

Public API
----------
- ``maybe_trigger_generation(topic)``   — the only call-site needs this.
- ``approved_question_count(topic)``    — useful for tests / diagnostics.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

_log = logging.getLogger("speedrun.auto_generator")

# Trigger generation when a topic has fewer unseen approved questions than this.
# The generator will produce enough questions to refill back to this target.
GENERATION_THRESHOLD = 10

# Overage multiplier: generate slightly more than the deficit to absorb eval
# failures.  e.g. 1.5 means if we need 4, we attempt 6.  Capped at
# GENERATION_THRESHOLD so a single batch never blows up.
_OVERAGE_FACTOR = 1.5

# Topics currently being generated — prevents duplicate concurrent threads.
_in_flight: set[str] = set()
_in_flight_lock = threading.Lock()

_QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"
_GENERATED_PATH = Path(__file__).resolve().parent / "generated_questions.json"


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def approved_question_count(topic: str) -> int:
    """Count approved questions for *topic* across both sources.

    "Approved" means: every question in questions.json, plus questions in
    generated_questions.json that have ``"eval_passed": true``.
    """
    return unseen_question_count(topic, set())


def unseen_question_count(topic: str, seen_ids: set[str | int]) -> int:
    """Count approved questions for *topic* that are not in *seen_ids*.

    Used to detect when the student is about to run out of fresh questions
    for a topic within the current session, so generation can start early.
    """
    import json

    count = 0
    try:
        from .performance_score import USE_MANUAL_QUESTIONS

        if USE_MANUAL_QUESTIONS and _QUESTIONS_PATH.exists():
            data = json.loads(_QUESTIONS_PATH.read_text(encoding="utf-8"))
            count += sum(
                1
                for q in data.get("questions", [])
                if q.get("topic") == topic and q.get("id") not in seen_ids
            )
    except Exception:  # noqa: BLE001
        pass
    try:
        if _GENERATED_PATH.exists():
            data = json.loads(_GENERATED_PATH.read_text(encoding="utf-8"))
            count += sum(
                1
                for q in data.get("questions", [])
                if q.get("topic") == topic
                and q.get("eval_passed", False)
                and q.get("id") not in seen_ids
            )
    except Exception:  # noqa: BLE001
        pass
    return count


def maybe_trigger_generation(
    topic: str | None,
    seen_ids: set[str | int] | None = None,
) -> None:
    """Enqueue background generation for *topic* when its unseen pool is low.

    *seen_ids* is the set of question IDs already served this session.
    The trigger fires when the number of *unseen* approved questions for
    the topic drops below ``GENERATION_THRESHOLD``, so new questions arrive
    before the student loops back to ones they have already answered.

    No-op when:
    - *topic* is None or empty.
    - Generation for this topic is already in progress.
    - The topic has >= GENERATION_THRESHOLD unseen approved questions.

    Returns immediately; generation happens on a daemon thread.
    """
    if not topic:
        return
    seen: set[str | int] = seen_ids or set()
    with _in_flight_lock:
        if topic in _in_flight:
            return
        count = unseen_question_count(topic, seen)
        if count >= GENERATION_THRESHOLD:
            return
        deficit = GENERATION_THRESHOLD - count
        to_generate = min(
            int(deficit * _OVERAGE_FACTOR + 0.999),  # ceiling with overage
            GENERATION_THRESHOLD,
        )
        _log.info(
            "Topic %r has %d unseen question(s) (threshold=%d) — "
            "generating %d to refill.",
            topic,
            count,
            GENERATION_THRESHOLD,
            to_generate,
        )
        _in_flight.add(topic)
    t = threading.Thread(
        target=_run,
        args=(topic, to_generate),
        daemon=True,
        name=f"speedrun-gen-{topic}",
    )
    t.start()


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


def _run(topic: str, count: int) -> None:
    """Top-level wrapper: run generation and always release _in_flight."""
    try:
        _generate(topic, count)
    except BaseException as exc:  # noqa: BLE001 — catch SystemExit too
        _log.warning("Auto-generation failed for %r: %s", topic, exc)
    finally:
        with _in_flight_lock:
            _in_flight.discard(topic)


def _generate(topic: str, count: int) -> None:
    """Full generation pipeline: fetch → generate → eval → save."""
    # ------------------------------------------------------------------ #
    # Step 1: check for API key (avoid the sys.exit inside _openai_client)
    # ------------------------------------------------------------------ #
    _load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        _log.info(
            "OPENAI_API_KEY not set — skipping auto-generation for %r.", topic
        )
        return

    # ------------------------------------------------------------------ #
    # Step 2: build OpenAI client
    # ------------------------------------------------------------------ #
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError:
        _log.warning("openai package not installed — skipping auto-generation.")
        return
    client = openai.OpenAI(api_key=api_key)

    # ------------------------------------------------------------------ #
    # Step 3: fetch source text from OpenStax / SEP
    # ------------------------------------------------------------------ #
    fetch_result = _fetch_source(topic)
    if fetch_result is None:
        _log.warning(
            "openstax_fetcher returned None for %r — skipping generation.", topic
        )
        return
    source_text, source_citation = fetch_result

    # ------------------------------------------------------------------ #
    # Step 4: generate questions
    # ------------------------------------------------------------------ #
    questions = _call_generator(
        client=client,
        topic=topic,
        source_text=source_text,
        source_citation=source_citation,
        count=count,
    )
    if not questions:
        _log.warning("Generator returned no questions for %r.", topic)
        return

    # ------------------------------------------------------------------ #
    # Step 5: eval → merge → save
    # ------------------------------------------------------------------ #
    _eval_and_save(questions, client, topic)


# ---------------------------------------------------------------------------
# Stage helpers
# ---------------------------------------------------------------------------


def _fetch_source(topic: str) -> tuple[str, str] | None:
    try:
        from .openstax_fetcher import fetch_topic

        return fetch_topic(topic)
    except Exception as exc:  # noqa: BLE001
        _log.warning("openstax_fetcher import/call failed for %r: %s", topic, exc)
        return None


def _call_generator(
    *,
    client: object,
    topic: str,
    source_text: str,
    source_citation: str,
    count: int,
) -> list[dict]:
    try:
        from .question_generator import generate_from_source

        return generate_from_source(
            client=client,
            topic=topic,
            source_text=source_text,
            source_citation=source_citation,
            count=count,
            num_examples=3,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("generate_from_source failed for %r: %s", topic, exc)
        return []


def _eval_and_save(
    questions: list[dict], client: object, topic: str
) -> None:
    """Run eval on *questions*, then merge-save to generated_questions.json."""
    try:
        from .eval import (
            run_eval,
            save_results,
        )
        from .eval import (
            save_generated as eval_save,
        )
    except ImportError:
        _log.warning("eval module unavailable — saving questions without eval.")
        _save_raw(questions)
        return

    try:
        annotated, summary = run_eval(questions, client, verbose=False)
    except Exception as exc:  # noqa: BLE001
        _log.warning("eval failed for %r: %s — saving without eval.", topic, exc)
        _save_raw(questions)
        return

    _merge_save(annotated, eval_save)

    try:
        passage_id = questions[0].get("passage_id", "—") if questions else "—"
        save_results(summary, passage_id=passage_id)
    except Exception:  # noqa: BLE001
        pass

    n_passed = summary.get("questions_passed", 0)
    _log.info(
        "Auto-generation complete for %r: %d generated, %d passed eval.",
        topic,
        len(questions),
        n_passed,
    )


def _save_raw(questions: list[dict]) -> None:
    """Append questions to generated_questions.json without eval metadata."""
    import json

    try:
        existing: list[dict] = []
        if _GENERATED_PATH.exists():
            data = json.loads(_GENERATED_PATH.read_text(encoding="utf-8"))
            existing = data.get("questions", [])
        merged = existing + questions
        _GENERATED_PATH.write_text(
            json.dumps({"questions": merged}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("_save_raw failed: %s", exc)


def _merge_save(annotated: list[dict], save_fn: object) -> None:
    """Merge newly annotated questions into generated_questions.json via *save_fn*."""
    import json

    try:
        existing: list[dict] = []
        if _GENERATED_PATH.exists():
            data = json.loads(_GENERATED_PATH.read_text(encoding="utf-8"))
            existing = data.get("questions", [])
        annotated_by_id = {q["id"]: q for q in annotated}
        merged = [annotated_by_id.get(q["id"], q) for q in existing]
        existing_ids = {q["id"] for q in existing}
        for q in annotated:
            if q["id"] not in existing_ids:
                merged.append(q)
        save_fn(merged)  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001
        _log.warning("_merge_save failed: %s", exc)
        return

    # Push each newly approved question to Firestore for cross-device sync.
    try:
        from .question_sync import push_question

        for q in annotated:
            if q.get("eval_passed", False):
                push_question(q)
    except Exception as exc:  # noqa: BLE001
        _log.debug("Firestore push skipped: %s", exc)


def _load_dotenv() -> None:
    """Load .env from the repo root into os.environ (no third-party deps)."""
    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
