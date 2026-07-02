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
import random
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
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (_REPO_ROOT / "pylib", _REPO_ROOT / "out" / "pylib"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    # Imported as part of the `speedrun` package (e.g. from the desktop UI).
    from speedrun import memory_score
except ImportError:
    # Run as a script: speedrun/ is on sys.path[0], so import the sibling.
    import memory_score  # type: ignore[import-not-found,no-redef]

# Below the threshold, no score is shown for a topic. Tunable: 3 suits the
# demo question bank (5 questions/topic).
# REMINDER: raise this toward 10-15 once more problems (incl. AI-generated
# questions) are added to speedrun/questions.json.
MIN_ANSWERED = 3

# Custom table added to the collection's SQLite database to store quiz answers.
PERFORMANCE_TABLE = "speedrun_performance"

QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"


@dataclass
class Question:
    """A single MCAT-style multiple choice question loaded from JSON."""

    id: int
    passage: str
    question: str
    choices: list[str]
    correct_answer: str
    topic: str
    concept: str


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Question(**q) for q in data["questions"]]


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
    def has_score(self) -> bool:
        return self.answered >= MIN_ANSWERED

    @property
    def needed(self) -> int:
        return max(0, MIN_ANSWERED - self.answered)

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
            answer_correct integer not null
        )
        """
    )


def record_answer(
    col: Collection,
    question: Question,
    chosen_concept: str,
    chosen_answer: str,
) -> tuple[bool, bool]:
    """Persist one two-step answer and return (concept_correct, answer_correct)."""
    ensure_table(col)
    concept_correct = 1 if chosen_concept == question.concept else 0
    answer_correct = 1 if chosen_answer == question.correct_answer else 0
    col.db.execute(
        f"""
        insert into {PERFORMANCE_TABLE}
            (answered_at, question_id, topic, concept, chosen_concept,
             correct_concept, chosen_answer, correct_answer,
             concept_correct, answer_correct)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        int(time.time()),
        question.id,
        question.topic,
        question.concept,
        chosen_concept,
        question.concept,
        chosen_answer,
        question.correct_answer,
        concept_correct,
        answer_correct,
    )
    return bool(concept_correct), bool(answer_correct)


def reset_answers(col: Collection) -> None:
    """Delete all stored answers (keeps the table)."""
    ensure_table(col)
    col.db.execute(f"delete from {PERFORMANCE_TABLE}")


def questions_by_id(path: Path = QUESTIONS_PATH) -> dict[int, Question]:
    return {q.id: q for q in load_questions(path)}


def _concept_options(
    question: Question,
    concepts_by_topic: dict[str, list[str]],
    all_concepts: list[str],
    rng: random.Random,
) -> list[str]:
    """Four concept choices for the concept-recognition step.

    The correct concept plus up to three distractors, preferring other concepts
    from the same topic before falling back to the global concept pool.
    """
    correct = question.concept
    same = [c for c in concepts_by_topic.get(question.topic, []) if c != correct]
    others = [c for c in all_concepts if c != correct and c not in same]
    rng.shuffle(same)
    rng.shuffle(others)

    distractors: list[str] = []
    for candidate in same + others:
        if candidate not in distractors:
            distractors.append(candidate)
        if len(distractors) == 3:
            break

    options = [*distractors, correct]
    rng.shuffle(options)
    return options


def questions_for_client(path: Path = QUESTIONS_PATH) -> list[dict]:
    """Question data for the in-app quiz, with correct answers withheld.

    Each question ships its four answer choices and four concept choices (for
    the concept-recognition step). The correct answer and correct concept are
    intentionally omitted so they cannot be read from the page source; grading
    happens server-side in `grade_question`.
    """
    questions = load_questions(path)
    concepts_by_topic: dict[str, list[str]] = {}
    all_concepts: list[str] = []
    for q in questions:
        concepts_by_topic.setdefault(q.topic, []).append(q.concept)
        if q.concept not in all_concepts:
            all_concepts.append(q.concept)

    client: list[dict] = []
    for q in questions:
        # Seed per question so the choice order is stable across re-renders.
        rng = random.Random(q.id)
        client.append(
            {
                "id": q.id,
                "passage": q.passage,
                "question": q.question,
                "choices": q.choices,
                "concept_choices": _concept_options(
                    q, concepts_by_topic, all_concepts, rng
                ),
                "topic": q.topic,
            }
        )
    return client


def grade_question(
    col: Collection, question_id: int, concept: str | None, answer: str | None
) -> dict:
    """Grade a two-step answer (concept + answer) and record the answer.

    ``concept`` and ``answer`` are the choice *texts* the student selected. Both
    verdicts are returned together so the UI can reveal them simultaneously; the
    answer is persisted for the performance score.
    """
    question = questions_by_id().get(question_id)
    if question is None:
        return {"error": f"unknown question id {question_id}"}

    concept_correct, answer_correct = record_answer(
        col, question, concept or "", answer or ""
    )
    return {
        "concept_correct": concept_correct,
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


def build_sections(results: dict[str, TopicResult]) -> list[SectionPerformance]:
    sections: list[SectionPerformance] = []
    for code in memory_score.SECTION_ORDER:
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
        f"Give-up rule: no score below {MIN_ANSWERED} answered questions per topic"
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
            lines.append(
                f"  Not enough data: {section.answered} answered. "
                f"Answer {section.needed} more to unlock this section's score."
            )

        lines.append("  Topic breakdown:")
        for t in section.topics:
            if t.answered == 0:
                lines.append(f"    - {t.name:<20} no answers yet")
            elif not t.has_score:
                lines.append(
                    f"    - {t.name:<20} {t.correct}/{t.answered} answered   "
                    f"(need {t.needed} more)"
                )
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
.mcat-perf { max-width: 720px; margin: 0 auto; padding: 20px 16px 40px; }
.mcat-perf h1 { font-size: 20px; margin: 0 0 4px; }
.mcat-perf .give-up-rule { opacity: 0.7; font-size: 13px; margin: 0 0 20px; }
.mcat-perf-section {
    border: 1px solid rgba(128, 128, 128, 0.3);
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 16px;
}
.mcat-perf-section .section-head { display: flex; align-items: baseline; gap: 8px; }
.mcat-perf-section .section-code { font-weight: 700; font-size: 15px; }
.mcat-perf-section .section-name { opacity: 0.7; font-size: 13px; }
.mcat-perf-score { margin: 10px 0 6px; }
.mcat-perf-score .value { font-size: 26px; font-weight: 700; }
.mcat-perf-score .count { opacity: 0.55; font-size: 12px; margin-left: 8px; }
.mcat-perf-nodata { margin: 10px 0 6px; opacity: 0.75; font-style: italic; }
table.mcat-perf-topics { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
table.mcat-perf-topics th, table.mcat-perf-topics td {
    text-align: left; padding: 5px 8px;
    border-top: 1px solid rgba(128, 128, 128, 0.2);
}
table.mcat-perf-topics th { opacity: 0.6; font-weight: 600; }
table.mcat-perf-topics td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.mcat-perf-topics td.muted { opacity: 0.5; }
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
        f"No score is shown for a topic with fewer than {MIN_ANSWERED} "
        "answered questions.</p>"
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
            elif not t.has_score:
                parts.append(
                    f"<tr><td>{_esc(t.name)}</td>"
                    f'<td class="num muted">need {t.needed} more</td>'
                    f'<td class="num">{t.correct}</td>'
                    f'<td class="num">{t.answered}</td></tr>'
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
.mcat-quiz-card {
    border: 1px solid rgba(128,128,128,0.3); border-radius: 8px;
    padding: 16px 18px; margin-top: 4px;
}
.mcat-quiz-progress { opacity: 0.6; font-size: 12px; margin-bottom: 8px; }
.mcat-quiz-topic { font-size: 12px; font-weight: 700; opacity: 0.7; text-transform: uppercase; letter-spacing: 0.04em; }
.mcat-quiz-passage { font-size: 13px; opacity: 0.85; margin: 8px 0; line-height: 1.5; }
.mcat-quiz-question { font-size: 15px; font-weight: 600; margin: 12px 0; line-height: 1.4; }
.mcat-quiz-choices { display: flex; flex-direction: column; gap: 8px; }
.mcat-choice {
    text-align: left; padding: 10px 12px; font-size: 14px; color: inherit;
    border: 1px solid rgba(128,128,128,0.4); border-radius: 6px;
    background: transparent; cursor: pointer; line-height: 1.4;
}
.mcat-choice:hover:not(:disabled) { background: rgba(125,125,255,0.12); }
.mcat-choice:disabled { cursor: default; }
.mcat-choice .letter { font-weight: 700; margin-right: 8px; }
.mcat-choice.correct { border-color: #2e9e5b; background: rgba(46,158,91,0.18); }
.mcat-choice.incorrect { border-color: #d64545; background: rgba(214,69,69,0.18); }
.mcat-quiz-prompt { font-size: 15px; font-weight: 700; margin: 14px 0 10px; }
.mcat-quiz-feedback { margin-top: 16px; font-size: 14px; }
.mcat-verdict-row { margin-top: 4px; }
.mcat-verdict-row .verdict { font-weight: 700; }
.mcat-verdict-row .verdict.correct { color: #2e9e5b; }
.mcat-verdict-row .verdict.incorrect { color: #d64545; }
.mcat-verdict-row .note { opacity: 0.75; font-size: 13px; }
.mcat-quiz-actions { margin-top: 16px; }
</style>
"""

# Two-step quiz engine for one block. Questions arrive pre-selected and
# pre-ordered from the loop. Step 1 asks the student to identify the concept
# being tested; step 2 asks for the answer. No feedback is shown between the two
# steps - the student commits to both independently - then both verdicts are
# revealed together. Grading is server-side via
# `pycmd("srq:grade:<url-encoded-json>")` (correct concept/answer never ship to
# the page); after the final question it calls `pycmd("sr:block_done")`.
QUIZ_JS = """
<script>
(function () {
    const QUESTIONS = %%QUESTIONS%%;
    const LETTERS = ["A", "B", "C", "D", "E", "F"];
    const quizEl = document.getElementById("sr-quiz");
    if (!quizEl) return;

    let idx = 0;
    let selectedConcept = null;

    function esc(s) {
        const d = document.createElement("div");
        d.textContent = s == null ? "" : String(s);
        return d.innerHTML;
    }

    function stem(q) {
        let html = '<div class="mcat-quiz-progress">Question ' + (idx + 1) +
                   ' of ' + QUESTIONS.length + '</div>';
        html += '<div class="mcat-quiz-topic">' + esc(q.topic) + '</div>';
        if (q.passage) {
            html += '<div class="mcat-quiz-passage">' + esc(q.passage) + '</div>';
        }
        html += '<div class="mcat-quiz-question">' + esc(q.question) + '</div>';
        return html;
    }

    function renderConcept() {
        const q = QUESTIONS[idx];
        selectedConcept = null;
        let html = '<div class="mcat-quiz-card">' + stem(q);
        html += '<div class="mcat-quiz-prompt">What concept is this question ' +
                'testing?</div>';
        html += '<div class="mcat-quiz-choices">';
        q.concept_choices.forEach(function (choice, i) {
            html += '<button class="mcat-choice" data-ci="' + i + '">' +
                    esc(choice) + '</button>';
        });
        html += '</div></div>';
        quizEl.innerHTML = html;

        quizEl.querySelectorAll(".mcat-choice").forEach(function (btn) {
            btn.addEventListener("click", function () {
                const ci = parseInt(btn.getAttribute("data-ci"), 10);
                selectedConcept = q.concept_choices[ci];
                renderAnswer();
            });
        });
    }

    function renderAnswer() {
        const q = QUESTIONS[idx];
        let html = '<div class="mcat-quiz-card">' + stem(q);
        html += '<div class="mcat-quiz-prompt">Now answer the question.</div>';
        html += '<div class="mcat-quiz-choices">';
        q.choices.forEach(function (choice, i) {
            html += '<button class="mcat-choice" data-i="' + i + '">' +
                    '<span class="letter">' + LETTERS[i] + '.</span>' +
                    esc(choice) + '</button>';
        });
        html += '</div>';
        html += '<div class="mcat-quiz-feedback" id="sr-feedback"></div>';
        html += '<div class="mcat-quiz-actions" id="sr-actions"></div>';
        html += '</div>';
        quizEl.innerHTML = html;

        quizEl.querySelectorAll(".mcat-choice").forEach(function (btn) {
            btn.addEventListener("click", function () {
                answer(parseInt(btn.getAttribute("data-i"), 10));
            });
        });
    }

    function verdictRow(label, ok, note) {
        const cls = ok ? "correct" : "incorrect";
        const word = ok ? "Correct" : "Incorrect";
        let row = '<div class="mcat-verdict-row">' +
                  '<span class="verdict ' + cls + '">' + label + ': ' +
                  word + '</span>';
        if (note) row += ' <span class="note">\u2014 ' + note + '</span>';
        return row + '</div>';
    }

    function answer(choiceIndex) {
        const q = QUESTIONS[idx];
        const payload = JSON.stringify({
            id: q.id, concept: selectedConcept, answer: q.choices[choiceIndex]
        });
        pycmd("srq:grade:" + encodeURIComponent(payload), function (res) {
            if (!res || res.error) return;

            const correctIdx = q.choices.indexOf(res.correct_answer);
            quizEl.querySelectorAll(".mcat-choice").forEach(function (btn) {
                const i = parseInt(btn.getAttribute("data-i"), 10);
                btn.disabled = true;
                if (i === correctIdx) btn.classList.add("correct");
                else if (i === choiceIndex) btn.classList.add("incorrect");
            });

            let fb = verdictRow("Concept", res.concept_correct,
                res.concept_correct ? "" :
                "the concept was: " + esc(res.correct_concept));
            fb += verdictRow("Answer", res.answer_correct,
                res.answer_correct ? "" :
                "the correct answer is " + LETTERS[correctIdx] + ".");
            document.getElementById("sr-feedback").innerHTML = fb;

            const last = idx === QUESTIONS.length - 1;
            const label = last ? "Finish block" : "Next question";
            const actions = document.getElementById("sr-actions");
            actions.innerHTML =
                '<button class="sr-btn" id="sr-next">' + label + '</button>';
            document.getElementById("sr-next").addEventListener("click", next);
        });
    }

    function next() {
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


def client_questions_for_ids(ids: list[int], path: Path = QUESTIONS_PATH) -> list[dict]:
    """Client question dicts (no correct answers) for the given ids, in order."""
    by_id = {q["id"]: q for q in questions_for_client(path)}
    return [by_id[i] for i in ids if i in by_id]


def render_question_block(questions: list[dict]) -> str:
    """Render the quiz body for one loop block over pre-selected questions."""
    # Escape "<" so an embedded string can never close the <script> element.
    questions_json = json.dumps(questions).replace("<", "\\u003c")
    js = QUIZ_JS.replace("%%QUESTIONS%%", questions_json)
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
        _, was_correct = record_answer(col, question, "", chosen)
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
