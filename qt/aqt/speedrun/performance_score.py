#!/usr/bin/env python3
"""Compute the Speedrun MCAT "performance score" from quiz answers.

Where the *memory* score (see `memory_score.py`) measures how well the FSRS
scheduler thinks a card is retained, the *performance* score measures how well
the student actually answers MCAT-style practice questions. It is defined per
topic as:

    accuracy = correct answers / total answered

Questions live in `speedrun/questions.json`; each carries a `topic` that is one
of the AnKing-MCAT subdecks (Behavioral, Biochemistry, Biology,
General-Chemistry, Organic-Chemistry). Topics roll up into the same three MCAT
sections used by the memory score, so the two scores line up side by side in
the Stats UI.

Answers are persisted to a dedicated `speedrun_performance` table inside the
Anki collection's own SQLite database (created on demand), so results survive
between sessions and can be read back by the desktop UI.

Give-up rule: a topic with fewer than MIN_ANSWERED answered questions shows
"not enough data" instead of a misleading accuracy. MIN_ANSWERED is 3 for the
small demo deck; a full question bank should raise it to 10-15.

This module is used two ways:

* As a CLI. `--quiz` presents questions and records answers (opens the *live*
  collection read-write, so Anki must be closed). With no arguments it prints a
  performance report from a temporary *copy* of the collection (safe to run
  while Anki is open).
* As a library imported by the Anki desktop UI (see qt/aqt/stats.py), which
  passes an already-open `Collection` to `compute_sections()` and renders the
  result with `render_html()`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

# Make the in-tree `anki` package importable when run as a standalone CLI, and
# make the sibling `memory_score` module importable in both run modes. See the
# equivalent bootstrap in memory_score.py for details.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
for _p in (_REPO_ROOT / "pylib", _REPO_ROOT / "out" / "pylib"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    # Imported as part of the `speedrun` package (e.g. from the desktop UI).
    from . import memory_score
except ImportError:
    # Run as a script: speedrun/ is on sys.path[0], so import the sibling.
    import memory_score  # type: ignore[import-not-found,no-redef]

# Section-level give-up rule for the performance score display, mirroring the
# memory score's SECTION_MIN_REVIEWED / MULTI_SUBDECK_MIN constants.
# A section needs at least SECTION_MIN_ANSWERED questions answered in total.
# Sections with 3 or more required topics (B/B: Biology/Biochemistry,
# C/P: G-Chem/Organic-Chem/Physics) additionally require at least
# MULTI_TOPIC_MIN_ANSWERED in each required topic.
SECTION_MIN_ANSWERED = 30
MULTI_TOPIC_MIN_ANSWERED = 10

# Topics that are supplemental/recommended. They still appear in the
# performance breakdown but are never counted toward the minimum threshold
# that blocks the readiness score.
OPTIONAL_TOPICS: frozenset[str] = frozenset({"Essential-Equations"})

# Kept for backward compat; the adaptive loop uses this lower bar to decide
# whether a topic has enough answers to weight it meaningfully.
MIN_ANSWERED = 5

# Custom table added to the collection's SQLite database to store quiz answers.
PERFORMANCE_TABLE = "speedrun_performance"

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"
GENERATED_PATH = Path(__file__).resolve().parent / "generated_questions.json"

# Set to False to skip questions.json entirely and serve only AI-generated
# questions (generated_questions.json with eval_passed: true).
# The file is kept on disk; flip back to True to restore it.
USE_MANUAL_QUESTIONS = False

# Per-topic: keep manual questions in the pool until at least this many
# AI-generated questions have passed eval for that topic.  Set to 1 so that
# manual questions are only shown when AI generation has produced nothing —
# i.e. the generator is broken or hasn't run yet.
MIN_GENERATED_PER_TOPIC = 1


@dataclass
class Question:
    """A single MCAT-style multiple choice question loaded from JSON."""

    id: str | int
    question: str
    choices: list[str]
    correct_answer: str
    topic: str
    # Optional / may not be present on every record.
    passage: str = ""
    passage_id: str = ""
    concept: str = ""
    rationale: str = ""
    source: str = ""
    source_citation: str = ""
    source_text: str = ""
    generated_by: str = ""
    format_reference: str = ""
    # True for questions that came from generated_questions.json and passed eval.
    ai_generated: bool = False


_QUESTION_FIELDS = {f.name for f in Question.__dataclass_fields__.values()}  # type: ignore[attr-defined]


def _questions_from_file(path: Path, *, ai_generated: bool = False) -> list[Question]:
    """Load questions from *path*, tagging each with *ai_generated*."""
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for q in data.get("questions", []):
        fields = {k: v for k, v in q.items() if k in _QUESTION_FIELDS}
        fields["ai_generated"] = ai_generated
        out.append(Question(**fields))
    return out


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    """Load questions, preferring AI-generated ones with per-topic manual fallback.

    Rules:
    • generated_questions.json — primary source; only eval_passed: true questions.
    • questions.json           — fallback at the topic level: included for any topic
                                 that has fewer than MIN_GENERATED_PER_TOPIC eval-passed
                                 generated questions, regardless of USE_MANUAL_QUESTIONS.
                                 This ensures topics like CARS are never left with a
                                 single generated question and no manual supplement.
                                 When USE_MANUAL_QUESTIONS is True, all manual
                                 questions are included as a global fallback too.
    • If generated_questions.json is missing or empty, all manual questions are used.
    • Deduplicated by id; generated questions win on collision.
    """
    seen: dict[str | int, Question] = {}
    generated_count_by_topic: dict[str, int] = {}

    # Primary: AI-generated questions
    try:
        if GENERATED_PATH.exists():
            raw = json.loads(GENERATED_PATH.read_text(encoding="utf-8"))
            for q in raw.get("questions", []):
                if not q.get("eval_passed", False):
                    continue
                qid: str | int = q.get("id", "")
                fields = {k: v for k, v in q.items() if k in _QUESTION_FIELDS}
                fields["ai_generated"] = True
                obj = Question(**fields)
                seen[qid] = obj
                generated_count_by_topic[obj.topic] = (
                    generated_count_by_topic.get(obj.topic, 0) + 1
                )
    except Exception:  # noqa: BLE001
        pass

    # Fallback: manual questions.json.
    # Always included when USE_MANUAL_QUESTIONS is True, when the generated pool
    # is completely empty, OR on a per-topic basis for any topic that has fewer
    # than MIN_GENERATED_PER_TOPIC eval-passed generated questions.
    if path.exists():
        try:
            for q in _questions_from_file(path, ai_generated=False):
                if q.id in seen:
                    continue  # generated question wins on collision
                topic_generated = generated_count_by_topic.get(q.topic, 0)
                topic_needs_fallback = topic_generated < MIN_GENERATED_PER_TOPIC
                if USE_MANUAL_QUESTIONS or not generated_count_by_topic or topic_needs_fallback:
                    seen[q.id] = q
        except Exception:  # noqa: BLE001
            pass

    return list(seen.values())


@dataclass
class TopicResult:
    """Accuracy accumulator for one topic (subdeck)."""

    name: str
    answered: int = 0
    correct: int = 0

    @property
    def has_score(self) -> bool:
        return self.answered >= MIN_ANSWERED

    @property
    def needed(self) -> int:
        return max(0, MIN_ANSWERED - self.answered)

    @property
    def accuracy(self) -> float:
        # Callers guard on answered > 0 (or has_score) before reading this.
        return self.correct / self.answered


@dataclass
class SectionPerformance:
    """Aggregated performance score for one MCAT section."""

    code: str
    name: str
    topics: list[TopicResult]

    @property
    def answered(self) -> int:
        return sum(t.answered for t in self.topics)

    @property
    def correct(self) -> int:
        return sum(t.correct for t in self.topics)

    @property
    def _required_topics(self) -> list["TopicResult"]:
        """Topics that count toward the minimum threshold (non-optional)."""
        return [t for t in self.topics if t.name not in OPTIONAL_TOPICS]

    @property
    def pending_topics(self) -> list[tuple[str, int]]:
        """Required topics still short of their per-topic minimum.

        The per-topic floor only applies when there are 3+ required topics
        (e.g. C/P with G-Chem / Organic / Physics).  With fewer required
        topics the section total is the only gate, so this returns empty and
        has_score just checks the overall answered count.
        """
        if len(self._required_topics) < 3:
            return []
        return [
            (t.name, MULTI_TOPIC_MIN_ANSWERED - t.answered)
            for t in self._required_topics
            if t.answered < MULTI_TOPIC_MIN_ANSWERED
        ]

    @property
    def has_score(self) -> bool:
        """True when every required topic meets its minimum AND section total >= 30."""
        return not self.pending_topics and self.answered >= SECTION_MIN_ANSWERED

    @property
    def needed(self) -> int:
        return max(0, SECTION_MIN_ANSWERED - self.answered)

    @property
    def accuracy(self) -> float:
        return self.correct / self.answered


# Storage
################


def ensure_table(col: Collection) -> None:
    """Create the performance table if it does not already exist."""
    col.db.execute(
        f"""
        create table if not exists {PERFORMANCE_TABLE} (
            id integer primary key,
            answered_at integer not null,
            question_id integer not null,
            topic text not null,
            concept text not null,
            chosen_concept text not null,
            correct_concept text not null,
            chosen_answer text not null,
            correct_answer text not null,
            concept_correct integer not null,
            application_correct integer not null default 0,
            answer_correct integer not null,
            sync_key text unique
        )
        """
    )
    # Migrate tables created before application_correct was added.
    try:
        col.db.execute(
            f"alter table {PERFORMANCE_TABLE} add column "
            f"application_correct integer not null default 0"
        )
    except Exception:  # noqa: BLE001 — column already exists
        pass
    # Migrate tables created before sync_key was added.
    # NOTE: SQLite does not allow UNIQUE in ALTER TABLE ADD COLUMN; the
    # uniqueness is enforced at the application level (uuid4 keys) and by
    # the CREATE TABLE definition for newly-created tables.
    try:
        col.db.execute(
            f"alter table {PERFORMANCE_TABLE} add column sync_key text"
        )
    except Exception:  # noqa: BLE001 — column already exists
        pass


def record_answer(
    col: Collection,
    question: Question,
    chosen_concept: str,
    chosen_answer: str,
    *,
    concept_correct: bool = False,
    application_correct: bool = False,
) -> tuple[bool, bool, bool]:
    """Persist one answer and return (concept_correct, application_correct, answer_correct).

    ``concept_correct`` and ``application_correct`` come from AI grading in the
    interactive quiz flow; the legacy CLI path leaves them False and relies on
    the caller to compute them before this call.
    """
    import uuid

    ensure_table(col)
    _letters = ["A", "B", "C", "D", "E", "F"]
    try:
        chosen_letter = _letters[question.choices.index(chosen_answer)]
    except (ValueError, IndexError):
        chosen_letter = ""
    answer_correct = 1 if chosen_letter == question.correct_answer else 0
    sync_key = uuid.uuid4().hex
    answered_at = int(time.time())
    col.db.execute(
        f"""
        insert into {PERFORMANCE_TABLE}
            (answered_at, question_id, topic, concept, chosen_concept,
             correct_concept, chosen_answer, correct_answer,
             concept_correct, application_correct, answer_correct, sync_key)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        answered_at,
        question.id,
        question.topic,
        question.concept,
        chosen_concept,
        question.concept,
        chosen_answer,
        question.correct_answer,
        int(concept_correct),
        int(application_correct),
        answer_correct,
        sync_key,
    )
    # Push to Firestore in a fire-and-forget thread so the quiz never stalls.
    _push_performance_async(
        {
            "sync_key": sync_key,
            "answered_at": answered_at,
            "question_id": str(question.id),
            "topic": question.topic,
            "concept": question.concept,
            "chosen_concept": chosen_concept,
            "correct_concept": question.concept,
            "chosen_answer": chosen_answer,
            "correct_answer": question.correct_answer,
            "concept_correct": int(concept_correct),
            "application_correct": int(application_correct),
            "answer_correct": answer_correct,
        }
    )
    return bool(concept_correct), bool(application_correct), bool(answer_correct)


def _push_performance_async(record: dict) -> None:
    """Push one performance record to Firestore in a daemon thread."""
    import threading

    def _run() -> None:
        try:
            from aqt.speedrun.question_sync import push_performance_record

            push_performance_record(record)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, daemon=True, name="speedrun-perf-push").start()


def grade_concept_with_ai(
    question: Question,
    response: str,
    api_key: str = "",
) -> dict:
    """AI-grade a free-response concept answer using GPT-4o.

    Returns a dict with ``concept_correct``, ``application_correct``, and
    ``feedback`` keys, or ``{"ai_unavailable": True}`` when the API is
    unreachable or the key is missing.
    """
    import re as _re

    if not api_key:
        return {"ai_unavailable": True}
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError:
        return {"ai_unavailable": True}
    try:
        client = openai.OpenAI(api_key=api_key)
        system = (
            "You are grading an MCAT student's concept recognition. "
            f"The correct concept is: {question.concept}. "
            "Mark concept_correct = true if the student's response names or clearly "
            "paraphrases the complete concept — including all key parts of a multi-part "
            "concept. Accept synonyms and different phrasings as long as the full idea is "
            "covered (exact repetition is also fine). "
            "Mark concept_correct = false if the student only names part of a multi-part "
            "concept, gives a vague generic answer (e.g. 'critical thinking', 'reading "
            "comprehension'), says they don't know, or identifies a clearly unrelated topic. "
            'Return JSON only (no markdown): '
            '{"concept_correct": true/false, "feedback": "one sentence explaining your decision"}'
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Student response: {response}"},
            ],
            temperature=0.2,
            timeout=30.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = _re.sub(r"^```(?:json)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw).strip()
        data = json.loads(raw)
        return {
            "concept_correct": bool(data.get("concept_correct", False)),
            "application_correct": False,
            "feedback": str(data.get("feedback", "")),
        }
    except Exception:  # noqa: BLE001
        return {"ai_unavailable": True}


def grade_answer_for_session(
    col: Collection,
    question_id: str | int,
    chosen_answer: str,
    *,
    concept_response: str = "",
    concept_correct: bool = False,
    application_correct: bool = False,
) -> dict:
    """Grade the MC answer, record all results to DB, and return the verdict.

    Called by the bridge after the student selects a choice in the quiz UI.
    ``concept_correct`` and ``application_correct`` were already determined by
    the prior ``grade_concept_with_ai`` call; they are stored together with the
    answer so the DB row captures the full question attempt.
    """
    question = questions_by_id().get(question_id)
    if question is None:
        return {"error": f"unknown question id {question_id}"}
    _, _, answer_correct = record_answer(
        col, question, concept_response, chosen_answer,
        concept_correct=concept_correct,
        application_correct=application_correct,
    )
    return {
        "answer_correct": answer_correct,
        "correct_answer": question.correct_answer,
        "concept_correct": concept_correct,
        "application_correct": application_correct,
        "correct_concept": question.concept,
        "rationale": question.rationale or "",
    }


def generate_explanation_with_ai(
    question: Question,
    chosen_answer: str,
    concept_correct: bool,
    application_correct: bool,
    answer_correct: bool,
    api_key: str = "",
) -> dict:
    """Generate a wrong-answer explanation using GPT-4o.

    Falls back to showing the rationale field directly when the API is
    unavailable or the call fails.  Always returns a dict with at least
    ``why_wrong``, ``key_takeaway``, and ``rationale`` keys.
    """
    import re as _re

    _letters = ["A", "B", "C", "D", "E", "F"]
    try:
        chosen_letter = _letters[question.choices.index(chosen_answer)]
    except (ValueError, IndexError):
        chosen_letter = "?"

    fallback: dict = {
        "concept_explanation": question.concept,
        "answer_explanation": question.rationale or question.concept,
        "fallback": True,
    }

    if not api_key:
        return fallback
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError:
        return fallback

    try:
        client = openai.OpenAI(api_key=api_key)
        sections: list[str] = []
        if not concept_correct:
            sections.append(
                '"concept_explanation": "one sentence defining the correct concept '
                'and why it applies here"'
            )
        if not answer_correct:
            sections.append(
                '"answer_explanation": "2-3 sentences: (a) why the student\'s chosen '
                f'answer ({chosen_letter} — {chosen_answer}) is wrong, and '
                "(b) why the correct answer is right, grounded in the rationale\""
            )
        json_shape = "{" + ", ".join(sections) + "}"
        prompt = (
            f"Question: {question.question}\n"
            f"Choices: {json.dumps(question.choices)}\n"
            f"Correct answer: {question.correct_answer}\n"
            f"Student chose: {chosen_letter} — {chosen_answer}\n"
            f"Concept: {question.concept}\n"
            f"Rationale: {question.rationale}\n\n"
            "Be direct and specific. "
            f"Return JSON only (no markdown): {json_shape}"
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You are an MCAT expert giving concise, constructive feedback.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            timeout=30.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = _re.sub(r"^```(?:json)?\s*", "", raw)
        raw = _re.sub(r"\s*```$", "", raw).strip()
        data = json.loads(raw)
        result: dict = {}
        if not concept_correct:
            result["concept_explanation"] = str(data.get("concept_explanation", question.concept))
        if not answer_correct:
            result["answer_explanation"] = str(
                data.get("answer_explanation", question.rationale or "")
            )
        return result
    except Exception:  # noqa: BLE001
        return fallback


def reset_answers(col: Collection) -> None:
    """Delete all stored answers (keeps the table)."""
    ensure_table(col)
    col.db.execute(f"delete from {PERFORMANCE_TABLE}")


def questions_by_id(path: Path = QUESTIONS_PATH) -> dict[str | int, Question]:
    return {q.id: q for q in load_questions(path)}


def questions_for_client(path: Path = QUESTIONS_PATH) -> list[dict]:
    """Question data for the in-app quiz, with correct answers withheld.

    The correct answer and correct concept are intentionally omitted so they
    cannot be read from the page source; grading happens server-side.
    """
    questions = load_questions(path)

    # Build a passage lookup so follow-on questions (same passage_id, no inline
    # passage text) can still display their shared passage.
    passage_by_id: dict[str, str] = {}
    for q in questions:
        if q.passage and q.passage_id:
            passage_by_id.setdefault(q.passage_id, q.passage)

    client: list[dict] = []
    for q in questions:
        passage = q.passage or (
            passage_by_id.get(q.passage_id, "") if q.passage_id else ""
        )
        entry: dict = {
            "id": q.id,
            "passage": passage,
            "question": q.question,
            "choices": q.choices,
            "topic": q.topic,
            "ai_generated": q.ai_generated,
            "source_citation": q.source_citation,
        }
        client.append(entry)
    return client


def grade_question(
    col: Collection, question_id: str | int, concept: str | None, answer: str | None
) -> dict:
    """Grade a two-step answer (concept + answer) and record the answer.

    ``concept`` and ``answer`` are the choice *texts* the student selected. Both
    verdicts are returned together so the UI can reveal them simultaneously; the
    answer is persisted for the performance score.
    """
    question = questions_by_id().get(question_id)
    if question is None:
        return {"error": f"unknown question id {question_id}"}

    # Legacy path: grade concept by string equality (CLI only).
    legacy_cc = bool((concept or "") == question.concept)
    _, _, answer_correct = record_answer(
        col, question, concept or "", answer or "",
        concept_correct=legacy_cc,
    )
    return {
        "concept_correct": legacy_cc,
        "answer_correct": answer_correct,
        "correct_answer": question.correct_answer,
        "correct_concept": question.concept,
    }


def fetch_topic_results(col: Collection) -> dict[str, TopicResult]:
    """Read stored answers and tally accuracy per topic."""
    ensure_table(col)
    results = {name: TopicResult(name) for name in memory_score.SUBDECK_TO_SECTION}
    rows = col.db.all(
        f"""
        select topic, count(*), coalesce(sum(answer_correct), 0)
        from {PERFORMANCE_TABLE}
        group by topic
        """
    )
    for topic, answered, correct in rows:
        if topic in results:
            results[topic].answered = int(answered)
            results[topic].correct = int(correct)
    return results


def fetch_topic_last_practiced(col: Collection) -> dict[str, float]:
    """Return days-ago since the most recent answered question per topic.

    Used by the adaptive loop to apply a recency factor to question weighting
    so well-practiced topics gradually drift back into rotation over time.
    Topics with no answers are absent from the returned dict.
    """
    import time

    ensure_table(col)
    rows = col.db.all(
        f"select topic, max(answered_at) from {PERFORMANCE_TABLE} group by topic"
    )
    now = time.time()
    return {
        topic: (now - float(ts)) / 86400.0
        for topic, ts in rows
        if ts is not None
    }


def build_sections(results: dict[str, TopicResult]) -> list[SectionPerformance]:
    # SECTION_ORDER covers B/B, C/P, P/S — all flashcard-backed sections.
    # CARS has no flashcard deck so it is absent from SECTION_ORDER, but it
    # does have practice questions and must appear in the performance display.
    section_codes = list(memory_score.SECTION_ORDER) + ["CARS"]
    sections: list[SectionPerformance] = []
    for code in section_codes:
        topics = [
            results[name]
            for name, section in memory_score.SUBDECK_TO_SECTION.items()
            if section == code
        ]
        sections.append(
            SectionPerformance(code, memory_score.SECTION_NAMES[code], topics)
        )
    return sections


def compute_sections(col: Collection) -> list[SectionPerformance]:
    """Convenience entry point for callers holding an open collection."""
    return build_sections(fetch_topic_results(col))


def fetch_results_from_copy(collection_path: Path) -> dict[str, TopicResult]:
    """Open a temporary copy of the collection and tally results (read-only).

    Used by the report CLI so it never locks or mutates the live collection
    (safe to run while Anki is open).
    """
    import shutil
    import tempfile

    from anki.collection import Collection

    tmpdir = tempfile.mkdtemp(prefix="speedrun-perfscore-")
    tmp_col = Path(tmpdir) / "collection.anki2"
    try:
        shutil.copy2(collection_path, tmp_col)
        col = Collection(str(tmp_col))
        try:
            return fetch_topic_results(col)
        finally:
            col.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# Rendering
################


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_text(sections: list[SectionPerformance]) -> str:
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("MCAT Performance Score  (quiz accuracy by section)")
    lines.append(
        f"Give-up rule: {SECTION_MIN_ANSWERED} answered questions per section; "
        f"sections with 3 topics also need >= {MULTI_TOPIC_MIN_ANSWERED} per topic"
    )
    lines.append("=" * 66)

    for section in sections:
        lines.append("")
        lines.append(f"{section.code} - {section.name}")
        lines.append("-" * 66)

        if section.has_score:
            lines.append(
                f"  Performance score: {pct(section.accuracy)}   "
                f"({section.correct}/{section.answered} correct)"
            )
        else:
            pending = ", ".join(
                f"{name} (needs {n} more)" for name, n in section.pending_topics
            )
            if pending:
                lines.append(f"  Not enough data. Answer more in: {pending}")
            else:
                lines.append(
                    f"  Not enough data: {section.answered} answered. "
                    f"Answer {section.needed} more to unlock this section's score."
                )

        lines.append("  Topic breakdown:")
        for t in section.topics:
            if t.answered == 0:
                lines.append(f"    - {t.name:<20} no answers yet")
            else:
                lines.append(
                    f"    - {t.name:<20} {pct(t.accuracy)}   "
                    f"({t.correct}/{t.answered} correct)"
                )

    lines.append("")
    lines.append("=" * 66)
    return "\n".join(lines)


_PERFORMANCE_CSS = """
<style>
.mcat-perf { max-width: 720px; margin: 0 auto; padding: 20px 16px 48px; }
.mcat-perf h1 { font-size: 22px; font-weight: 800; margin: 0 0 4px; letter-spacing: -0.02em; }
.mcat-perf .give-up-rule { opacity: 0.55; font-size: 13px; margin: 0 0 24px; line-height: 1.6; }
.mcat-perf-section {
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 16px;
    background: rgba(255,255,255,0.025);
}
.mcat-perf-section .section-head { display: flex; align-items: baseline; gap: 8px; }
.mcat-perf-section .section-code { font-weight: 800; font-size: 15px; }
.mcat-perf-section .section-name { opacity: 0.55; font-size: 13px; }
.mcat-perf-score { margin: 10px 0 6px; }
.mcat-perf-score .value { font-size: 28px; font-weight: 800; letter-spacing: -0.02em; }
.mcat-perf-score .count { opacity: 0.45; font-size: 12px; margin-left: 8px; }
.mcat-perf-nodata { margin: 10px 0 6px; opacity: 0.55; font-style: italic; font-size: 13px; }
table.mcat-perf-topics { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 13px; }
table.mcat-perf-topics th, table.mcat-perf-topics td {
    text-align: left; padding: 6px 10px;
    border-top: 1px solid rgba(255,255,255,0.07);
}
table.mcat-perf-topics th { opacity: 0.45; font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
table.mcat-perf-topics td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.mcat-perf-topics td.muted { opacity: 0.4; }
</style>
"""


def _esc(text: str) -> str:
    from html import escape

    return escape(text)


def render_html(sections: list[SectionPerformance]) -> str:
    """Render the performance-score report as an HTML body for an AnkiWebView."""
    parts: list[str] = [_PERFORMANCE_CSS, '<div class="mcat-perf">']
    parts.append("<h1>MCAT Performance Score</h1>")
    parts.append(
        '<p class="give-up-rule">Quiz accuracy by section. '
        f"Each section needs at least {SECTION_MIN_ANSWERED} answered questions total. "
        f"Sections with 3 topics (B/B, C/P) also require at least "
        f"{MULTI_TOPIC_MIN_ANSWERED} per topic. "
        "Otherwise the section shows &ldquo;Not enough data&rdquo;.</p>"
    )

    for section in sections:
        parts.append('<div class="mcat-perf-section">')
        parts.append(
            '<div class="section-head">'
            f'<span class="section-code">{_esc(section.code)}</span>'
            f'<span class="section-name">{_esc(section.name)}</span>'
            "</div>"
        )

        if section.has_score:
            parts.append(
                '<div class="mcat-perf-score">'
                f'<span class="value">{pct(section.accuracy)}</span>'
                f'<span class="count">({section.correct}/{section.answered} '
                "correct)</span>"
                "</div>"
            )
        else:
            pending = section.pending_topics
            if pending:
                pending_str = ", ".join(
                    f"{_esc(name)} (needs {n} more)" for name, n in pending
                )
                parts.append(
                    '<div class="mcat-perf-nodata">'
                    f"Not enough data. Answer more in: {pending_str}."
                    "</div>"
                )
            else:
                parts.append(
                    '<div class="mcat-perf-nodata">'
                    f"Not enough data: {section.answered} answered. "
                    f"Answer {section.needed} more to unlock this section&rsquo;s score."
                    "</div>"
                )

        parts.append('<table class="mcat-perf-topics">')
        parts.append(
            '<tr><th>Topic</th><th class="num">Accuracy</th>'
            '<th class="num">Correct</th><th class="num">Answered</th></tr>'
        )
        for t in section.topics:
            if t.answered == 0:
                parts.append(
                    f"<tr><td>{_esc(t.name)}</td>"
                    '<td class="num muted" colspan="2">no answers yet</td>'
                    '<td class="num muted">0</td></tr>'
                )
            else:
                parts.append(
                    f"<tr><td>{_esc(t.name)}</td>"
                    f'<td class="num">{pct(t.accuracy)}</td>'
                    f'<td class="num">{t.correct}</td>'
                    f'<td class="num">{t.answered}</td></tr>'
                )
        parts.append("</table>")
        parts.append("</div>")

    parts.append("</div>")
    return "\n".join(parts)


# Question-block UI (served inside the Speedrun loop, see qt/aqt/speedrun.py)
################

QUIZ_CSS = """
<style>
@keyframes fadeIn {
    from { opacity: 0; transform: translateY(6px); }
    to   { opacity: 1; transform: translateY(0); }
}
.mcat-quiz-card {
    border: 1px solid rgba(255,255,255,0.07); border-radius: 14px;
    padding: 20px 22px; margin-top: 4px;
    background: rgba(255,255,255,0.025);
    animation: fadeIn 0.22s ease;
}
/* Progress bar */
.mcat-progress-wrap { margin-bottom: 14px; }
.mcat-progress-bar {
    height: 4px; border-radius: 2px; overflow: hidden;
    background: rgba(255,255,255,0.08); margin-bottom: 8px;
}
.mcat-progress-fill {
    height: 100%; border-radius: 2px;
    background: linear-gradient(90deg, #7c6ef5, #9d8fff);
    transition: width 0.35s ease;
}
.mcat-progress-row { display: flex; align-items: center; gap: 0; }
.mcat-progress-text { opacity: 0.4; font-size: 12px; letter-spacing: 0.02em; }
.mcat-streak {
    margin-left: 10px; font-size: 12px; font-weight: 700;
    color: #fbbf24; animation: fadeIn 0.25s ease;
}
/* Topic / badges */
.mcat-quiz-topic {
    font-size: 11px; font-weight: 700; opacity: 0.4;
    text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px;
}
.mcat-ai-badge {
    display: inline-block; font-size: 10px; font-weight: 700;
    letter-spacing: 0.06em; text-transform: uppercase;
    color: #9d8fff; border: 1px solid rgba(124,110,245,0.4);
    background: rgba(124,110,245,0.1);
    border-radius: 20px; padding: 2px 8px; margin: 2px 0 4px;
}
.mcat-source-citation { font-size: 11px; opacity: 0.4; margin-top: 4px; font-style: italic; }
/* Passage */
.mcat-quiz-passage { font-size: 13px; opacity: 0.8; margin: 8px 0; line-height: 1.6; }
.mcat-passage-toggle-wrap { margin: 8px 0 4px; }
.mcat-passage-toggle {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.1);
    border-radius: 20px; padding: 4px 12px; font-size: 12px; cursor: pointer;
    color: inherit; font-family: inherit;
    transition: opacity 0.15s, background 0.15s;
}
.mcat-passage-toggle:hover { background: rgba(255,255,255,0.09); }
.mcat-passage-full {
    margin-top: 8px; font-size: 13px; line-height: 1.6; opacity: 0.8;
    border-left: 3px solid rgba(124,110,245,0.5); padding-left: 12px;
}
/* Question */
.mcat-quiz-question { font-size: 16px; font-weight: 600; margin: 12px 0 16px; line-height: 1.45; }
.mcat-quiz-prompt {
    font-size: 11px; font-weight: 700; opacity: 0.45;
    text-transform: uppercase; letter-spacing: 0.05em; margin: 14px 0 10px;
}
/* Choices */
.mcat-quiz-choices { display: flex; flex-direction: column; gap: 8px; }
.mcat-choice {
    text-align: left; padding: 11px 14px; font-size: 14px; color: inherit;
    border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;
    background: rgba(255,255,255,0.03); cursor: pointer; line-height: 1.4;
    transition: border-color 0.18s, background 0.18s;
    font-family: inherit;
}
.mcat-choice:hover:not(:disabled) { background: rgba(124,110,245,0.1); border-color: rgba(124,110,245,0.45); }
.mcat-choice:disabled { cursor: default; }
.mcat-choice .letter { font-weight: 700; margin-right: 10px; color: rgba(255,255,255,0.45); }
.mcat-choice.correct  { border-color: rgba(34,197,94,0.5); background: rgba(34,197,94,0.1); }
.mcat-choice.correct .letter { color: #4ade80; }
.mcat-choice.incorrect { border-color: rgba(248,113,113,0.5); background: rgba(248,113,113,0.1); }
.mcat-choice.incorrect .letter { color: #f87171; }
/* Concept input */
.mcat-concept-input {
    width: 100%; min-height: 80px; box-sizing: border-box;
    padding: 11px 14px; font-size: 14px; font-family: inherit; color: inherit;
    border: 1px solid rgba(255,255,255,0.1); border-radius: 10px;
    background: rgba(255,255,255,0.04); resize: vertical; line-height: 1.5; margin: 4px 0 12px;
    transition: border-color 0.18s;
}
.mcat-concept-input:focus { outline: none; border-color: rgba(124,110,245,0.55); }
.mcat-concept-input:disabled { opacity: 0.4; }
/* Feedback */
.mcat-quiz-feedback { margin-top: 14px; font-size: 14px; animation: fadeIn 0.22s ease; }
.mcat-verdict-row { margin-top: 6px; display: flex; align-items: baseline; gap: 8px; }
.mcat-verdict-row .verdict {
    font-weight: 700; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.05em; padding: 2px 9px; border-radius: 20px; flex-shrink: 0;
}
.mcat-verdict-row .verdict.correct {
    color: #4ade80; background: rgba(34,197,94,0.12); border: 1px solid rgba(34,197,94,0.28);
}
.mcat-verdict-row .verdict.incorrect {
    color: #f87171; background: rgba(248,113,113,0.12); border: 1px solid rgba(248,113,113,0.28);
}
.mcat-verdict-row .note { opacity: 0.7; font-size: 13px; }
/* Explanation panel */
.mcat-explanation {
    margin-top: 12px; padding: 14px 16px;
    border: 1px solid rgba(255,255,255,0.07); border-radius: 10px;
    background: rgba(255,255,255,0.03); font-size: 13px; line-height: 1.65;
}
.mcat-explanation-row { margin-bottom: 10px; }
.mcat-explanation-row:last-child { margin-bottom: 0; }
.mcat-explanation-label {
    font-weight: 700; opacity: 0.5; font-size: 10px;
    text-transform: uppercase; letter-spacing: 0.06em;
    display: block; margin-bottom: 3px;
}
/* Review card */
.mcat-review-divider {
    border: none; border-top: 1px solid rgba(255,255,255,0.07); margin: 16px 0;
}
.mcat-review-stem {
    font-size: 14px; font-weight: 600; line-height: 1.45;
    opacity: 0.9; margin-bottom: 14px;
}
/* Actions row */
.mcat-quiz-actions { margin-top: 18px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.mcat-thinking { opacity: 0.45; font-size: 12px; font-style: italic; }
.sr-btn-secondary {
    background: rgba(255,255,255,0.05); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 20px; padding: 8px 16px; font-size: 13px;
    cursor: pointer; color: inherit; font-family: inherit;
    transition: background 0.18s, border-color 0.18s;
}
.sr-btn-secondary:hover { background: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.2); }
</style>
"""

# Three-step quiz engine for one block.
#
# Step 1 — Concept recognition (free response, AI-graded):
#   Show passage + question stem + text box. On submit, call
#   pycmd("srq:grade_concept:…") which grades with GPT-4o and stores the
#   result server-side, then reveals the answer choices.
#   If AI is unavailable (%%AI_AVAILABLE%% == false), skip to step 2 directly.
#
# Step 2 — Multiple choice:
#   Student selects an answer; pycmd("srq:grade_answer:…") records to DB and
#   returns all verdicts.
#
# Step 3 — Explanation (shown only if anything was wrong):
#   pycmd("srq:explain:…") calls GPT-4o for a tailored explanation, falling
#   back to the rationale field if the API is unavailable.
#   If everything was correct, skip explanation and go straight to next question.
#
# Correct answers/concepts never ship to the page; grading is always server-side.
# After the final question, pycmd("sr:block_done") is called.
QUIZ_JS = """
<script>
(function () {
    const QUESTIONS = %%QUESTIONS%%;
    const AI_AVAILABLE = %%AI_AVAILABLE%%;
    const LETTERS = ["A", "B", "C", "D", "E", "F"];
    const quizEl = document.getElementById("sr-quiz");
    if (!quizEl) return;

    let idx = 0;
    let conceptResult = null;
    let consecutiveCorrect = 0;
    // Stored so the Review card's Back button can restore the result view.
    let _lastGrade = null; // { res, conceptWasGraded, choiceIndex, correctIdx }

    function esc(s) {
        const d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    }

    // ── Progress bar + streak ─────────────────────────────────────────────────

    function progressHtml() {
        const pct = Math.round((idx / QUESTIONS.length) * 100);
        let streak = "";
        if (consecutiveCorrect >= 2) {
            streak = '<span class="mcat-streak">\U0001F525 ' +
                     consecutiveCorrect + ' in a row!</span>';
        }
        return '<div class="mcat-progress-wrap">' +
               '<div class="mcat-progress-bar">' +
               '<div class="mcat-progress-fill" style="width:' + pct + '%"></div></div>' +
               '<div class="mcat-progress-row">' +
               '<span class="mcat-progress-text">Question ' + (idx + 1) +
               ' of ' + QUESTIONS.length + '</span>' + streak +
               '</div></div>';
    }

    // ── Question stem (passage full or collapsed) ─────────────────────────────

    function stem(q, collapsePassage) {
        let html = progressHtml();
        html += '<div class="mcat-quiz-topic">' + esc(q.topic) + '</div>';
        if (q.ai_generated) {
            html += '<div class="mcat-ai-badge">AI-generated</div>';
        }
        if (q.passage) {
            if (collapsePassage) {
                html += '<div class="mcat-passage-toggle-wrap">' +
                        '<button class="mcat-passage-toggle" id="sr-passage-btn">' +
                        '\U0001F4DA Show passage</button>' +
                        '<div id="sr-passage-full" class="mcat-passage-full" style="display:none">' +
                        esc(q.passage) + '</div></div>';
            } else {
                html += '<div class="mcat-quiz-passage">' + esc(q.passage) + '</div>';
            }
        }
        html += '<div class="mcat-quiz-question">' + esc(q.question) + '</div>';
        if (q.ai_generated && q.source_citation) {
            html += '<div class="mcat-source-citation">Source: ' +
                    esc(q.source_citation) + '</div>';
        }
        return html;
    }

    function attachPassageToggle() {
        const btn = document.getElementById("sr-passage-btn");
        if (!btn) return;
        btn.addEventListener("click", function () {
            const panel = document.getElementById("sr-passage-full");
            if (!panel) return;
            const open = panel.style.display !== "none";
            panel.style.display = open ? "none" : "block";
            btn.textContent = open ? "\U0001F4DA Show passage"
                                   : "\U0001F4DA Hide passage";
        });
    }

    // ── Step 1: concept free response ────────────────────────────────────────

    function renderConcept() {
        conceptResult = null;
        _lastGrade = null;
        if (!AI_AVAILABLE) { renderAnswer(); return; }
        const q = QUESTIONS[idx];
        let html = '<div class="mcat-quiz-card">' + stem(q, false);
        html += '<div class="mcat-quiz-prompt">What concept is this question testing?</div>';
        html += '<textarea class="mcat-concept-input" id="sr-concept-input" ' +
                'placeholder="Type your response\u2026" rows="3"></textarea>';
        html += '<div class="mcat-quiz-actions" id="sr-concept-actions">' +
                '<button class="sr-btn" id="sr-concept-submit">Submit</button>' +
                '</div></div>';
        quizEl.innerHTML = html;
        const ta = document.getElementById("sr-concept-input");
        ta.focus();
        document.getElementById("sr-concept-submit").addEventListener("click",
            function () { submitConcept(ta.value.trim()); });
        ta.addEventListener("keydown", function (e) {
            if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
                submitConcept(ta.value.trim());
            }
        });
    }

    function submitConcept(response) {
        document.getElementById("sr-concept-input").disabled = true;
        document.getElementById("sr-concept-actions").innerHTML =
            '<span class="mcat-thinking">Analyzing your response\u2026</span>';
        const q = QUESTIONS[idx];
        const payload = JSON.stringify({ id: q.id, response: response });
        pycmd("srq:grade_concept:" + encodeURIComponent(payload), function (res) {
            conceptResult = (res && !res.error) ? res : { ai_unavailable: true };
            renderAnswer();
        });
    }

    // ── Step 2: multiple choice (passage collapsed) ───────────────────────────

    function renderAnswer() {
        const q = QUESTIONS[idx];
        let html = '<div class="mcat-quiz-card">' + stem(q, !!q.passage);
        if (AI_AVAILABLE) {
            html += '<div class="mcat-quiz-prompt">Now select the best answer.</div>';
        }
        html += '<div class="mcat-quiz-choices">';
        q.choices.forEach(function (choice, i) {
            html += '<button class="mcat-choice" data-i="' + i + '">' +
                    '<span class="letter">' + LETTERS[i] + '.</span>' +
                    esc(choice) + '</button>';
        });
        html += '</div><div class="mcat-quiz-feedback" id="sr-feedback"></div>' +
                '<div class="mcat-quiz-actions" id="sr-actions"></div></div>';
        quizEl.innerHTML = html;
        attachPassageToggle();
        quizEl.querySelectorAll(".mcat-choice").forEach(function (btn) {
            btn.addEventListener("click", function () {
                submitAnswer(parseInt(btn.getAttribute("data-i"), 10));
            });
        });
    }

    function submitAnswer(choiceIndex) {
        const q = QUESTIONS[idx];
        quizEl.querySelectorAll(".mcat-choice").forEach(function (b) { b.disabled = true; });
        document.getElementById("sr-actions").innerHTML =
            '<span class="mcat-thinking">Grading\u2026</span>';

        const cc = conceptResult ? !!conceptResult.concept_correct : false;
        const ac = conceptResult ? !!conceptResult.application_correct : false;
        const payload = JSON.stringify({
            id: q.id, answer: q.choices[choiceIndex],
            concept_correct: cc, application_correct: ac,
        });
        pycmd("srq:grade_answer:" + encodeURIComponent(payload), function (res) {
            if (!res || res.error) {
                document.getElementById("sr-actions").innerHTML = "";
                var fb = "";
                if (res && res.fallback) {
                    fb += verdictRow("Answer", false,
                        "correct answer is " + esc(res.correct_answer) + ".");
                    fb += explanationHtml("", res.rationale);
                } else {
                    fb = '<div class="mcat-verdict-row">' +
                         '<span class="verdict incorrect">Could not record answer \u2014 please try again.</span></div>';
                }
                document.getElementById("sr-feedback").innerHTML = fb;
                showNextButton();
                return;
            }

            const correctIdx = LETTERS.indexOf(res.correct_answer);
            quizEl.querySelectorAll(".mcat-choice").forEach(function (btn) {
                const i = parseInt(btn.getAttribute("data-i"), 10);
                if (i === correctIdx) btn.classList.add("correct");
                else if (i === choiceIndex) btn.classList.add("incorrect");
            });

            const conceptWasGraded = !!res.concept_was_graded;
            const anythingWrong = !res.answer_correct ||
                (conceptWasGraded && !res.concept_correct);

            // Update streak.
            if (!anythingWrong) {
                consecutiveCorrect++;
            } else {
                consecutiveCorrect = 0;
            }

            _lastGrade = { res: res, conceptWasGraded: conceptWasGraded,
                           choiceIndex: choiceIndex, correctIdx: correctIdx };

            showVerdicts(res, conceptWasGraded, correctIdx);

            if (anythingWrong) {
                // Offer a Review card instead of inline explanation.
                document.getElementById("sr-actions").innerHTML =
                    '<button class="sr-btn" id="sr-review">Review explanation \u2192</button>';
                document.getElementById("sr-review").addEventListener("click",
                    function () { renderReview(); });
            } else {
                showNextButton();
            }
        });
    }

    // ── Step 3: Review card ───────────────────────────────────────────────────

    function renderReview() {
        if (!_lastGrade) { advance(); return; }
        const { res, conceptWasGraded, choiceIndex, correctIdx } = _lastGrade;
        const q = QUESTIONS[idx];

        let html = '<div class="mcat-quiz-card">' + progressHtml();
        html += '<div class="mcat-quiz-topic">' + esc(q.topic) + '</div>';
        // Collapsed passage available for reference.
        if (q.passage) {
            html += '<div class="mcat-passage-toggle-wrap">' +
                    '<button class="mcat-passage-toggle" id="sr-passage-btn">' +
                    '\U0001F4DA Show passage</button>' +
                    '<div id="sr-passage-full" class="mcat-passage-full" style="display:none">' +
                    esc(q.passage) + '</div></div>';
        }
        html += '<div class="mcat-review-stem">' + esc(q.question) + '</div>';
        html += '<hr class="mcat-review-divider">';

        // Verdicts.
        html += '<div class="mcat-quiz-feedback">';
        html += verdictRowsHtml(res, conceptWasGraded, correctIdx);
        const conceptFeedback = (conceptWasGraded && !res.concept_correct && conceptResult && conceptResult.feedback)
            ? conceptResult.feedback : "";
        html += explanationHtml(conceptFeedback, res.rationale);
        html += '</div>';

        html += '<div class="mcat-quiz-actions">';
        html += '<button class="sr-btn-secondary" id="sr-back">\u2190 Back to question</button>';
        html += nextBtnHtml();
        html += '</div></div>';

        quizEl.innerHTML = html;
        attachPassageToggle();

        document.getElementById("sr-back").addEventListener("click", function () {
            restoreResult();
        });
        attachNextButton();
    }

    function restoreResult() {
        if (!_lastGrade) { renderAnswer(); return; }
        const { res, conceptWasGraded, choiceIndex, correctIdx } = _lastGrade;
        renderAnswer(); // re-renders clean MC card
        // Re-apply highlights.
        quizEl.querySelectorAll(".mcat-choice").forEach(function (btn) {
            btn.disabled = true;
            const i = parseInt(btn.getAttribute("data-i"), 10);
            if (i === correctIdx) btn.classList.add("correct");
            else if (i === choiceIndex) btn.classList.add("incorrect");
        });
        showVerdicts(res, conceptWasGraded, correctIdx);
        document.getElementById("sr-actions").innerHTML =
            '<button class="sr-btn" id="sr-review">Review explanation \u2192</button>';
        document.getElementById("sr-review").addEventListener("click",
            function () { renderReview(); });
    }

    // ── Shared helpers ────────────────────────────────────────────────────────

    function showVerdicts(res, conceptWasGraded, correctIdx) {
        document.getElementById("sr-feedback").innerHTML =
            verdictRowsHtml(res, conceptWasGraded, correctIdx);
    }

    function verdictRowsHtml(res, conceptWasGraded, correctIdx) {
        let html = "";
        if (conceptWasGraded) {
            const note = res.concept_correct ? ""
                : "the correct concept is " + res.correct_concept;
            html += verdictRow("Concept", res.concept_correct, note);
        }
        html += verdictRow("Answer", res.answer_correct,
            res.answer_correct ? "" : "correct answer is " + LETTERS[correctIdx] + ".");
        return html;
    }

    function verdictRow(label, ok, note) {
        const cls  = ok ? "correct" : "incorrect";
        const word = ok ? "Correct" : "Incorrect";
        let row = '<div class="mcat-verdict-row"><span class="verdict ' + cls + '">' +
                  label + ': ' + word + '</span>';
        if (note) row += ' <span class="note">\u2014 ' + esc(note) + '</span>';
        return row + '</div>';
    }

    function explanationHtml(conceptFeedback, rationale) {
        if (!conceptFeedback && !rationale) return "";
        let html = '<div class="mcat-explanation">';
        if (conceptFeedback) {
            html += '<div class="mcat-explanation-row">' +
                    '<span class="mcat-explanation-label">Concept: </span>' +
                    esc(conceptFeedback) + '</div>';
        }
        if (rationale) {
            html += '<div class="mcat-explanation-row">' +
                    '<span class="mcat-explanation-label">Explanation: </span>' +
                    esc(rationale) + '</div>';
        }
        return html + '</div>';
    }

    function nextBtnHtml() {
        const last = idx === QUESTIONS.length - 1;
        return '<button class="sr-btn" id="sr-next">' +
               (last ? "Finish block" : "Next question \u2192") + '</button>';
    }

    function attachNextButton() {
        const btn = document.getElementById("sr-next");
        if (btn) btn.addEventListener("click", advance);
    }

    function showNextButton() {
        document.getElementById("sr-actions").innerHTML = nextBtnHtml();
        attachNextButton();
    }

    function advance() {
        if (idx < QUESTIONS.length - 1) {
            idx++;
            renderConcept();
        } else {
            pycmd("sr:block_done");
        }
    }

    renderConcept();
})();
</script>
"""


def client_questions_for_ids(ids: list[str | int], path: Path = QUESTIONS_PATH) -> list[dict]:
    """Client question dicts (no correct answers) for the given ids, in order."""
    by_id = {q["id"]: q for q in questions_for_client(path)}
    return [by_id[i] for i in ids if i in by_id]


def render_question_block(questions: list[dict], *, ai_available: bool = False) -> str:
    """Render the quiz body for one loop block over pre-selected questions."""
    # Escape "<" so an embedded string can never close the <script> element.
    questions_json = json.dumps(questions).replace("<", "\\u003c")
    js = QUIZ_JS.replace("%%QUESTIONS%%", questions_json)
    js = js.replace("%%AI_AVAILABLE%%", "true" if ai_available else "false")
    return "\n".join([QUIZ_CSS, '<div id="sr-quiz"></div>', js])


# Interactive quiz (CLI)
################

_LETTERS = ["A", "B", "C", "D", "E", "F"]


def _prompt_question(question: Question) -> str | None:
    """Show one question and return the chosen answer text, or None to quit."""
    print("\n" + "=" * 70)
    print(f"[{question.topic}] Question {question.id}")
    print("-" * 70)
    if question.passage:
        print(question.passage)
        print()
    print(question.question)
    print()
    for letter, choice in zip(_LETTERS, question.choices):
        print(f"  {letter}. {choice}")
    valid = _LETTERS[: len(question.choices)]
    while True:
        raw = input(f"\nYour answer ({'/'.join(valid)}, or q to quit): ").strip()
        if raw.lower() in ("q", "quit", "exit"):
            return None
        letter = raw.upper()
        if letter in valid:
            return question.choices[valid.index(letter)]
        print("  Please enter one of:", ", ".join(valid), "(or q to quit).")


def run_quiz(
    col: Collection,
    topics: list[str] | None = None,
    limit: int | None = None,
) -> None:
    """Present questions interactively and record answers to the collection."""
    questions = load_questions()
    if topics:
        wanted = {t.lower() for t in topics}
        questions = [q for q in questions if q.topic.lower() in wanted]
    if limit is not None:
        questions = questions[:limit]

    if not questions:
        print("No questions match the requested filter.")
        return

    print(f"Starting quiz: {len(questions)} question(s). Enter 'q' to stop early.")
    answered = 0
    correct = 0
    for question in questions:
        chosen = _prompt_question(question)
        if chosen is None:
            break
        _, _, was_correct = record_answer(col, question, "", chosen)
        answered += 1
        correct += int(was_correct)
        if was_correct:
            print("  \u2713 Correct.")
        else:
            print(f"  \u2717 Incorrect. Correct answer: {question.correct_answer}")
        print(f"  Concept: {question.concept}")

    print("\n" + "=" * 70)
    if answered:
        print(
            f"Session complete: {correct}/{answered} correct "
            f"({pct(correct / answered)})."
        )
    else:
        print("Session complete: no questions answered.")
    print("=" * 70)


def open_live_collection(collection_path: Path) -> Collection:
    """Open the real collection read-write (fails if Anki has it locked)."""
    from anki.collection import Collection

    try:
        return Collection(str(collection_path))
    except Exception as exc:
        sys.exit(
            f"error: could not open the live collection at {collection_path}\n"
            f"  ({exc})\n"
            "  Close the Anki desktop app before running the quiz, since it "
            "locks the collection."
        )


def print_report(sections: list[SectionPerformance]) -> None:
    print(render_text(sections))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--collection",
        help="Path to collection.anki2 (defaults to the standard Anki location)",
    )
    parser.add_argument(
        "--quiz",
        action="store_true",
        help="Present questions and record answers (opens the live collection; "
        "close Anki first).",
    )
    parser.add_argument(
        "--topic",
        action="append",
        dest="topics",
        help="Restrict the quiz to a topic (repeatable), e.g. --topic Biology.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Maximum number of questions to present in the quiz.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete all stored quiz answers, then exit.",
    )
    args = parser.parse_args()

    collection_path = memory_score.resolve_collection_path(args.collection)

    if args.reset:
        col = open_live_collection(collection_path)
        try:
            reset_answers(col)
        finally:
            col.close()
        print("Cleared all stored performance answers.")
        return

    if args.quiz:
        col = open_live_collection(collection_path)
        try:
            run_quiz(col, topics=args.topics, limit=args.limit)
            print_report(build_sections(fetch_topic_results(col)))
        finally:
            col.close()
        return

    print(f"Reading collection: {collection_path}")
    results = fetch_results_from_copy(collection_path)
    print_report(build_sections(results))


if __name__ == "__main__":
    main()
