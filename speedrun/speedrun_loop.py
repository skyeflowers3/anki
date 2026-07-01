#!/usr/bin/env python3
"""Three-mode adaptive learning loop for Speedrun.

When a student studies an MCAT deck, instead of a plain Anki review session they
enter this loop.

Scope decides the overall shape of the loop:

* General deck (more than one topic in scope): every block is mixed across
  topics and weighted by points-at-stake (section weight x weakness), for both
  questions and flashcards, so weaker high-value topics get more items in each
  block. Stats are re-read each block, so a topic's share updates as the student
  improves. Mixed question blocks only include question-eligible topics; when
  none are eligible yet, a mixed flashcard block is served instead.
* Single subdeck (one topic in scope): the focused loop below runs on that one
  topic.

Every session opens with a flashcard block (mixed for the general deck, or the
highest points-at-stake topic for a subdeck) so memory is refreshed before any
scores steer the loop; for a single subdeck the loop then alternates between
three modes:

* Mode 1 - Interleaved discovery: serve mixed-topic question blocks, weighted by
  points-at-stake (section weight x weakness). This is the default starting
  mode. After each block, check every topic for a gap; if one is found, move to
  Mode 2 focused on the highest points-at-stake gapped topic.
* Mode 2 - Focused remediation: serve blocks focused on the single gapped topic.
  Flashcards are served until the topic is question-eligible (>= 20 reviewed
  cards AND memory > 75%). Once eligible, question blocks are served and the
  next block is chosen by the *block-level* result pattern (see
  ``route_after_question_block``). No more than two question blocks are served
  in a row before returning to flashcards. The topic leaves remediation when the
  block pattern is mostly concept-right/answer-right, or when a flashcard block
  leaves it solid (eligible AND performance > 65%).

Question blocks are only ever served for question-eligible topics. A topic below
either bar (too few reviewed cards, or memory <= 75%) always gets flashcard
blocks, so the student builds memory before application practice begins.
* Mode 3 - Interleaved consolidation: one interleaved block that includes the
  freshly remediated topic, then return to Mode 1.

Practice questions are two-step: the student first identifies the concept being
tested, then answers the question, and both are graded together. Two signals
drive the loop, both read from the live collection:

* memory  - FSRS retrievability per topic (see memory_score.py).
* performance - quiz accuracy per topic (see performance_score.py).

This module holds only the *decision* logic (which block to serve next, and how
the mode changes after a block). It is deliberately free of any Qt/UI code so it
can be unit-tested in isolation; the desktop driver lives in qt/aqt/speedrun.py.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (_REPO_ROOT / "pylib", _REPO_ROOT / "out" / "pylib"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

try:
    from speedrun import memory_score, performance_score
except ImportError:
    import memory_score  # type: ignore[import-not-found,no-redef]
    import performance_score  # type: ignore[import-not-found,no-redef]

# --- Tunable thresholds -----------------------------------------------------

# Switching a topic from flashcards to questions requires an *established*
# memory score: the student must have reviewed at least this many cards AND
# have memory above the memory bar. Below either, always serve flashcards.
QUESTION_REVIEWED_THRESHOLD = 20
QUESTION_MEMORY_THRESHOLD = 0.75
# A topic at/above the memory bar but below this performance has an
# *application* gap (knows the material but can't apply it to questions).
APPLICATION_PERFORMANCE_THRESHOLD = 0.55
# An application gap is considered closed once performance rises above this.
APPLICATION_CLEARED_THRESHOLD = 0.65
# Never serve more than this many question blocks in a row on one topic without
# returning to flashcards (anti-loop guard).
MAX_CONSECUTIVE_QUESTION_BLOCKS = 2

# Block sizes.
FLASHCARD_BLOCK_SIZE = 10  # knowledge remediation (spec: 8-10 cards)
QUESTION_BLOCK_SIZE = 5  # interleaved / application blocks (spec: 4-5 questions)

# Per-subdeck points-at-stake weights, mirroring the Rust scheduler's
# SECTION_WEIGHTS (rslib/src/storage/card/mod.rs). Points at stake for a topic
# is weight * weakness, where weakness = 1 - memory.
POINTS_WEIGHTS: dict[str, float] = {
    "Behavioral": 1.0,
    "Biology": 0.5,
    "Biochemistry": 0.5,
    "General-Chemistry": 0.33,
    "Organic-Chemistry": 0.33,
    "Physics-and-Math": 0.33,
    "Essential-Equations": 0.15,
}


class Mode(Enum):
    INTERLEAVED_DISCOVERY = 1
    FOCUSED_REMEDIATION = 2
    INTERLEAVED_CONSOLIDATION = 3


class GapType(Enum):
    KNOWLEDGE = "knowledge"
    APPLICATION = "application"


@dataclass
class TopicStats:
    """Snapshot of one topic's memory and performance signals."""

    name: str
    section: str
    weight: float
    memory: float | None  # FSRS retrievability, None if no reviewed cards
    reviewed: int
    performance: float | None  # quiz accuracy, None if below MIN_ANSWERED
    answered: int

    @property
    def weakness(self) -> float:
        # Unknown memory is treated as maximally weak so undiscovered topics
        # rise to the top during interleaved discovery.
        return 1.0 - (self.memory if self.memory is not None else 0.0)

    @property
    def points_at_stake(self) -> float:
        return self.weight * self.weakness

    @property
    def has_memory_score(self) -> bool:
        """Whether a usable memory score exists (>= MIN_REVIEWED cards)."""
        return self.memory is not None

    @property
    def is_question_eligible(self) -> bool:
        """Whether the topic may be served as a question block.

        Application practice only makes sense once memory is established: the
        student must have reviewed at least ``QUESTION_REVIEWED_THRESHOLD`` cards
        AND have memory above ``QUESTION_MEMORY_THRESHOLD``. Below either bar the
        topic gets flashcards first.
        """
        return (
            self.memory is not None
            and self.reviewed >= QUESTION_REVIEWED_THRESHOLD
            and self.memory > QUESTION_MEMORY_THRESHOLD
        )

    @property
    def has_knowledge_gap(self) -> bool:
        # Anything not yet question-eligible (too few cards or weak memory) is a
        # knowledge gap: the student must build memory with flashcards first.
        return not self.is_question_eligible

    @property
    def has_application_gap(self) -> bool:
        return (
            self.is_question_eligible
            and self.performance is not None
            and self.performance < APPLICATION_PERFORMANCE_THRESHOLD
        )

    @property
    def gap_type(self) -> GapType | None:
        if self.has_knowledge_gap:
            return GapType.KNOWLEDGE
        if self.has_application_gap:
            return GapType.APPLICATION
        return None

    def gap_cleared(self) -> bool:
        """Whether the topic's current gap (if any) is now closed."""
        if self.has_knowledge_gap or self.has_application_gap:
            return False
        return True


@dataclass
class BlockPlan:
    """A description of the next block to serve."""

    kind: str  # "questions" or "flashcards"
    mode: Mode
    reason: str  # short, student-facing line shown before the block
    topic: str | None = None
    size: int = 0
    question_ids: list[int] = field(default_factory=list)
    gap_type: GapType | None = None


def build_topic_stats(col: Collection, topics: list[str]) -> dict[str, TopicStats]:
    """Read memory + performance for each in-scope topic from the collection."""
    buckets = memory_score.fetch_buckets(col)
    perf = performance_score.fetch_topic_results(col)

    stats: dict[str, TopicStats] = {}
    for name in topics:
        section = memory_score.SUBDECK_TO_SECTION.get(name, "")
        bucket = buckets.get(name)
        reviewed = bucket.reviewed if bucket else 0
        # A topic only has a memory score once it clears the same give-up rule
        # the memory report uses (MIN_REVIEWED cards with FSRS data). Below that,
        # memory is unknown and the topic needs flashcards to build recall.
        has_memory = bucket is not None and reviewed >= memory_score.MIN_REVIEWED
        memory = bucket.average if has_memory else None

        result = perf.get(name)
        answered = result.answered if result else 0
        performance = result.accuracy if result and result.has_score else None

        stats[name] = TopicStats(
            name=name,
            section=section,
            weight=POINTS_WEIGHTS.get(name, 0.0),
            memory=memory,
            reviewed=reviewed,
            performance=performance,
            answered=answered,
        )
    return stats


def highest_priority_gap(
    stats: dict[str, TopicStats], exclude: set[str] | None = None
) -> TopicStats | None:
    """Return the gapped topic with the highest points-at-stake, if any."""
    exclude = exclude or set()
    gapped = [
        t for t in stats.values() if t.gap_type is not None and t.name not in exclude
    ]
    if not gapped:
        return None
    return max(gapped, key=lambda t: (t.points_at_stake, t.name))


def highest_points_at_stake(
    stats: dict[str, TopicStats], exclude: set[str] | None = None
) -> TopicStats | None:
    """Return the topic with the highest points-at-stake (weakest x weightiest)."""
    exclude = exclude or set()
    candidates = [t for t in stats.values() if t.name not in exclude]
    if not candidates:
        return None
    return max(candidates, key=lambda t: (t.points_at_stake, t.name))


def _questions_by_topic(topics: list[str]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {name: [] for name in topics}
    for q in performance_score.load_questions():
        if q.topic in grouped:
            grouped[q.topic].append(q.id)
    return grouped


def weighted_interleave(
    pools: dict[str, list[int]],
    stats: dict[str, TopicStats],
    size: int,
    *,
    include_topic: str | None = None,
    rng: random.Random | None = None,
) -> list[int]:
    """Pick a mixed block of ids, weighting topics by points-at-stake.

    ``pools`` maps a topic to the ids available for it (question ids or card
    ids). Weaker high-value topics (higher points-at-stake) are drawn more often
    within the block, so e.g. a student weak in Biochemistry and strong in
    Behavioral gets more Biochemistry items in the same block. Topics with no
    remaining ids are skipped. If ``include_topic`` has ids, at least one of them
    is guaranteed in the block (used to re-surface a just-remediated topic).
    """
    rng = rng or random.Random()
    # Copy so we can consume ids without repeats inside a block.
    pools = {t: list(ids) for t, ids in pools.items() if ids}
    for ids in pools.values():
        rng.shuffle(ids)

    def stake(topic: str) -> float:
        ts = stats.get(topic)
        return max(ts.points_at_stake, 1e-6) if ts is not None else 1e-6

    chosen: list[int] = []
    if include_topic and pools.get(include_topic):
        chosen.append(pools[include_topic].pop())

    while len(chosen) < size:
        available = [t for t, ids in pools.items() if ids]
        if not available:
            break
        weights = [stake(t) for t in available]
        topic = rng.choices(available, weights=weights, k=1)[0]
        chosen.append(pools[topic].pop())
    return chosen


def select_interleaved_questions(
    stats: dict[str, TopicStats],
    questions_by_topic: dict[str, list[int]],
    size: int,
    *,
    include_topic: str | None = None,
    rng: random.Random | None = None,
) -> list[int]:
    """Pick a mixed block of question ids, weighting topics by points-at-stake.

    Only topics with an established memory score are eligible for question
    blocks; no-memory / weak-memory topics are handled with flashcards instead,
    so they never appear here.
    """
    eligible = {t for t, s in stats.items() if s.is_question_eligible}
    pools = {t: ids for t, ids in questions_by_topic.items() if t in eligible}
    return weighted_interleave(
        pools, stats, size, include_topic=include_topic, rng=rng
    )


def select_topic_questions(
    topic: str,
    questions_by_topic: dict[str, list[int]],
    size: int,
    *,
    rng: random.Random | None = None,
) -> list[int]:
    """Pick up to ``size`` question ids from a single topic."""
    rng = rng or random.Random()
    ids = list(questions_by_topic.get(topic, []))
    rng.shuffle(ids)
    return ids[:size]


@dataclass
class BlockOutcome:
    """Result of a completed block, handed back to :meth:`after_block`.

    ``results`` holds one ``(concept_correct, answer_correct)`` pair per question
    for question blocks, and is empty for flashcard blocks.
    """

    kind: str  # "questions" or "flashcards"
    results: list[tuple[bool, bool]] = field(default_factory=list)


def route_after_question_block(results: list[tuple[bool, bool]]) -> str:
    """Decide what to serve next based on the *block-level* pattern of results.

    Returns one of ``"flashcards"``, ``"another_question"``, ``"next_topic"``.
    Routing looks at which quadrant more than half the block fell into:

    * concept wrong & answer wrong  -> flashcards (rebuild the fundamentals)
    * concept right & answer wrong  -> another question block (then flashcards if
      still struggling, enforced by the consecutive-block guard)
    * concept wrong & answer right  -> another question block (concept practice)
    * concept right & answer right  -> move on to the next topic
    """
    n = len(results)
    if n == 0:
        return "flashcards"
    half = n / 2
    cw_aw = sum(1 for c, a in results if not c and not a)
    cr_aw = sum(1 for c, a in results if c and not a)
    cw_ar = sum(1 for c, a in results if not c and a)
    cr_ar = sum(1 for c, a in results if c and a)

    if cw_aw > half:
        return "flashcards"
    if cr_aw > half:
        return "another_question"
    if cw_ar > half:
        return "another_question"
    if cr_ar > half:
        return "next_topic"

    # No clear majority: fall back to overall answer accuracy.
    if (cw_ar + cr_ar) > half:
        return "next_topic"
    return "another_question"


class SpeedrunSession:
    """Drives the three-mode loop for one study session.

    The desktop driver calls :meth:`plan_block` to learn what to serve next,
    presents it, then calls :meth:`after_block` once the block is finished so
    the mode can advance. The two signals (memory, performance) are re-read from
    the collection for every decision, so each block selection runs fresh.
    """

    def __init__(self, topics: list[str], rng: random.Random | None = None) -> None:
        self.topics = topics
        self.rng = rng or random.Random()
        # More than one topic in scope (the general MCAT deck was selected):
        # serve mixed, points-at-stake-weighted blocks and re-weight each block,
        # rather than the focused single-topic loop used for a single subdeck.
        self.mixed = len(topics) > 1
        self.mode = Mode.INTERLEAVED_DISCOVERY
        self.focus_topic: str | None = None
        self.focus_gap: GapType | None = None
        self.remediated_topic: str | None = None
        self.started = False
        # Consecutive question blocks served on the current focus topic, and the
        # block type the last routing decision forced next (anti-loop guard).
        self.q_streak = 0
        self.next_kind: str | None = None
        # Topics with no reviewable cards; skipped so the loop can't hang on them.
        self.exhausted_topics: set[str] = set()

    def mark_topic_exhausted(self, topic: str | None) -> None:
        if topic:
            self.exhausted_topics.add(topic)

    # -- planning ------------------------------------------------------------

    def plan_block(self, stats: dict[str, TopicStats]) -> BlockPlan | None:
        """Decide the next block. None if there is nothing left to serve."""
        questions_by_topic = _questions_by_topic(self.topics)

        # Every session opens with a flashcard block, so the student warms up
        # memory before any scores are used to steer the loop.
        if not self.started:
            self.started = True
            if self.mixed:
                return self._mixed_flashcard_plan()
            opener = self._plan_opening_flashcards(stats)
            if opener is not None:
                return opener

        # General deck: always serve mixed, points-at-stake-weighted blocks.
        if self.mixed:
            return self._plan_mixed(stats, questions_by_topic)

        if self.mode is Mode.FOCUSED_REMEDIATION and self.focus_topic:
            return self._plan_remediation(stats, questions_by_topic)

        include = (
            self.remediated_topic
            if self.mode is Mode.INTERLEAVED_CONSOLIDATION
            else None
        )
        ids = select_interleaved_questions(
            stats,
            questions_by_topic,
            QUESTION_BLOCK_SIZE,
            include_topic=include,
            rng=self.rng,
        )
        if not ids:
            # No topic has recall built yet, so there is nothing to serve as a
            # question block. Drop into focused flashcard remediation for the
            # highest points-at-stake knowledge gap instead.
            return self._fallback_to_flashcards(stats, questions_by_topic)

        if self.mode is Mode.INTERLEAVED_CONSOLIDATION and self.remediated_topic:
            reason = f"Let's lock it in with a mix of questions, including {self.remediated_topic}."
        else:
            reason = "Let's practice with a mix of questions across your topics."
        return BlockPlan(
            kind="questions",
            mode=self.mode,
            reason=reason,
            size=len(ids),
            question_ids=ids,
        )

    def _plan_opening_flashcards(
        self, stats: dict[str, TopicStats]
    ) -> BlockPlan | None:
        topic = highest_points_at_stake(stats, exclude=self.exhausted_topics)
        if topic is None:
            return None
        return BlockPlan(
            kind="flashcards",
            mode=self.mode,
            reason=f"{topic.name} flashcards",
            topic=topic.name,
            size=FLASHCARD_BLOCK_SIZE,
            gap_type=GapType.KNOWLEDGE,
        )

    def _plan_mixed(
        self,
        stats: dict[str, TopicStats],
        questions_by_topic: dict[str, list[int]],
    ) -> BlockPlan | None:
        """A mixed, points-at-stake-weighted block across all in-scope topics.

        Serves a mixed question block when any topic is question-eligible,
        otherwise a mixed flashcard block. Re-reads stats every call, so the
        per-topic weighting updates after each block.
        """
        ids = select_interleaved_questions(
            stats, questions_by_topic, QUESTION_BLOCK_SIZE, rng=self.rng
        )
        if ids:
            return BlockPlan(
                kind="questions",
                mode=Mode.INTERLEAVED_DISCOVERY,
                reason="Mixed practice questions",
                size=len(ids),
                question_ids=ids,
            )
        return self._mixed_flashcard_plan()

    def _mixed_flashcard_plan(self) -> BlockPlan | None:
        """A mixed flashcard block; the driver picks weighted cards per topic."""
        if not self.topics:
            return None
        return BlockPlan(
            kind="flashcards",
            mode=Mode.INTERLEAVED_DISCOVERY,
            reason="Mixed flashcards",
            topic=None,
            size=FLASHCARD_BLOCK_SIZE,
            gap_type=GapType.KNOWLEDGE,
        )

    def _remediation_kind(self, topic_stats: TopicStats) -> str:
        """Flashcards or questions for the focused topic, deciding fresh."""
        # Build memory first: below the reviewed/memory bar, always flashcards.
        if not topic_stats.is_question_eligible:
            return "flashcards"
        # Anti-loop guard: never more than N question blocks in a row.
        if self.q_streak >= MAX_CONSECUTIVE_QUESTION_BLOCKS:
            return "flashcards"
        # Routing from the previous question block may force flashcards.
        if self.next_kind == "flashcards":
            return "flashcards"
        return "questions"

    def _flashcard_plan(self, topic: str) -> BlockPlan:
        self.q_streak = 0
        self.next_kind = None
        self.focus_gap = GapType.KNOWLEDGE
        return BlockPlan(
            kind="flashcards",
            mode=self.mode,
            reason=f"{topic} flashcards",
            topic=topic,
            size=FLASHCARD_BLOCK_SIZE,
            gap_type=GapType.KNOWLEDGE,
        )

    def _plan_remediation(
        self,
        stats: dict[str, TopicStats],
        questions_by_topic: dict[str, list[int]],
    ) -> BlockPlan | None:
        assert self.focus_topic is not None
        topic = self.focus_topic
        topic_stats = stats.get(topic)
        if topic_stats is None:
            return None

        if self._remediation_kind(topic_stats) == "flashcards":
            return self._flashcard_plan(topic)

        ids = select_topic_questions(
            topic, questions_by_topic, QUESTION_BLOCK_SIZE, rng=self.rng
        )
        if not ids:
            # Topic is memory-ready but has no questions to practise; skip it so
            # the loop doesn't get stuck, and re-plan from discovery.
            self.exhausted_topics.add(topic)
            self.mode = Mode.INTERLEAVED_DISCOVERY
            self.focus_topic = None
            self.focus_gap = None
            self.q_streak = 0
            self.next_kind = None
            return self.plan_block(stats)

        self.q_streak += 1
        self.focus_gap = GapType.APPLICATION
        return BlockPlan(
            kind="questions",
            mode=self.mode,
            reason=f"You know your {topic}. Now let's try some practice questions.",
            topic=topic,
            size=len(ids),
            question_ids=ids,
            gap_type=GapType.APPLICATION,
        )

    def _fallback_to_flashcards(
        self,
        stats: dict[str, TopicStats],
        questions_by_topic: dict[str, list[int]],
    ) -> BlockPlan | None:
        """Interleaved mode has no question-eligible topics: remediate instead.

        Enters focused remediation on the highest points-at-stake knowledge gap
        so the student builds recall before any question practice.
        """
        gap = highest_priority_gap(stats, exclude=self.exhausted_topics)
        if gap is None or gap.gap_type is None:
            return None
        self._enter_remediation(gap)
        return self._plan_remediation(stats, questions_by_topic)

    def _enter_remediation(self, gap: TopicStats) -> None:
        self.mode = Mode.FOCUSED_REMEDIATION
        self.focus_topic = gap.name
        self.focus_gap = gap.gap_type
        self.q_streak = 0
        self.next_kind = None

    # -- transitions ---------------------------------------------------------

    def after_block(
        self,
        stats: dict[str, TopicStats],
        outcome: BlockOutcome | None = None,
    ) -> None:
        """Advance the mode after a completed block, using fresh stats."""
        # Mixed mode has no mode transitions: the next block simply re-weights
        # from fresh stats, so weaker topics get more items next time.
        if self.mixed:
            return
        if self.mode is Mode.INTERLEAVED_DISCOVERY:
            self._after_discovery(stats)
        elif self.mode is Mode.FOCUSED_REMEDIATION:
            self._after_remediation(stats, outcome)
        elif self.mode is Mode.INTERLEAVED_CONSOLIDATION:
            self._after_consolidation()

    def _after_discovery(self, stats: dict[str, TopicStats]) -> None:
        gap = highest_priority_gap(stats, exclude=self.exhausted_topics)
        if gap is not None:
            self._enter_remediation(gap)

    def _after_remediation(
        self, stats: dict[str, TopicStats], outcome: BlockOutcome | None
    ) -> None:
        if self.focus_topic is None:
            self.mode = Mode.INTERLEAVED_DISCOVERY
            return
        topic = stats.get(self.focus_topic)
        if topic is None:
            self._enter_consolidation()
            return

        if outcome is not None and outcome.kind == "questions":
            # Route on the block-level pattern of concept/answer correctness.
            decision = route_after_question_block(outcome.results)
            if decision == "next_topic":
                self._enter_consolidation()
            elif decision == "flashcards":
                self.next_kind = "flashcards"
            else:  # another_question
                self.next_kind = "questions"
            return

        # A flashcard block finished: reset the question streak and move on only
        # once the topic is solid (memory established and performance cleared).
        self.q_streak = 0
        self.next_kind = None
        if self._topic_solid(topic):
            self._enter_consolidation()

    def _enter_consolidation(self) -> None:
        self.remediated_topic = self.focus_topic
        self.focus_topic = None
        self.focus_gap = None
        self.q_streak = 0
        self.next_kind = None
        self.mode = Mode.INTERLEAVED_CONSOLIDATION

    def _after_consolidation(self) -> None:
        # One interleaved session, then back to discovery.
        self.mode = Mode.INTERLEAVED_DISCOVERY
        self.remediated_topic = None

    def _topic_solid(self, topic: TopicStats) -> bool:
        """Whether a focused topic is done: memory established and performance
        cleared, so it can leave remediation for consolidation."""
        return (
            topic.is_question_eligible
            and topic.performance is not None
            and topic.performance > APPLICATION_CLEARED_THRESHOLD
        )
