# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Desktop driver for the Speedrun three-mode adaptive learning loop.

When a student studies an MCAT deck, this module takes over from the standard
review session and runs the loop defined in speedrun/speedrun_loop.py:

* Two-step question blocks and between-block transition screens render inline in
  the main window (``mw.web``) via a custom ``speedrun`` main-window state, so the
  loop feels like a native part of Anki rather than a dialog layered on top.
* Flashcard blocks are delegated to Anki's own reviewer, scoped to the topic's
  subdeck and capped at a block size via the ``reviewer_did_answer_card`` hook,
  after which control returns here.

The pure decision logic lives in the shared ``speedrun_loop`` module; this file
only wires that logic to the Qt UI and the scheduler.
"""

from __future__ import annotations

import logging
import random
import sys
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

_log = logging.getLogger("speedrun.qt")

import aqt
from anki.decks import DeckId
from aqt import gui_hooks
from aqt.qt import *

if TYPE_CHECKING:
    import aqt.main


MCAT_ROOT = "MCAT Study Blocks"

# A session is a fixed number of blocks; after the last one a summary is shown.
SESSION_BLOCKS = 5

# Number of questions in a practice quiz session.
PRACTICE_QUIZ_SIZE = 20

# Custom main-window state, so the loop's screens render inline in mw.web (the
# same web view the reviewer uses) instead of a separate dialog window. Typed as
# a plain str since it is not one of Anki's built-in MainWindowState literals.
SPEEDRUN_STATE: str = "speedrun"


def _modules():
    """Import the shared speedrun logic modules (see stats.py for rationale)."""
    try:
        from aqt.speedrun import performance_score, speedrun_loop
    except ModuleNotFoundError:
        repo_root = Path(aqt.__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from aqt.speedrun import performance_score, speedrun_loop

    return speedrun_loop, performance_score


def scope_topics(mw: aqt.main.AnkiQt, deck_id: DeckId) -> dict[str, DeckId | None]:
    """Map each in-scope topic (subdeck) to a deck id, within the selection.

    Topics are the MCAT Study Blocks subdecks known to the loop (Behavioral,
    Biochemistry, ...). Reviewing a topic's deck naturally includes any of its
    child decks.

    Questions-only topics (e.g. CARS) that have no flashcard deck are included
    with a ``None`` deck id so question blocks can serve them even though no
    flashcard review is possible.
    """
    speedrun_loop, _ = _modules()
    known = speedrun_loop.POINTS_WEIGHTS
    root = mw.col.decks.get(deck_id)
    if not root:
        return {}
    root_name = root["name"]

    topics: dict[str, DeckId | None] = {}
    for entry in mw.col.decks.all_names_and_ids(
        skip_empty_default=True, include_filtered=False
    ):
        name = entry.name
        if name != root_name and not name.startswith(f"{root_name}::"):
            continue
        leaf = name.split("::")[-1]
        if leaf in known and leaf not in topics:
            topics[leaf] = DeckId(entry.id)

    # Deeper selection (e.g. a sub-subdeck): fall back to any topic component in
    # the selected deck's own path, mapped to its ancestor deck.
    if not topics:
        parts = root_name.split("::")
        for i, comp in enumerate(parts):
            if comp in known:
                ancestor = "::".join(parts[: i + 1])
                did = mw.col.decks.id_for_name(ancestor)
                if did is not None:
                    topics[comp] = did

    # Inject questions-only topics (no flashcard deck) so they participate in
    # question blocks even when there is no matching deck in the collection.
    _, _perf = _modules()
    _ms = speedrun_loop.memory_score  # already imported inside speedrun_loop
    for name in _ms.QUESTIONS_ONLY_TOPICS:
        if name in known and name not in topics:
            topics[name] = None  # no deck; flashcard blocks skip it

    return topics


# Rendering
############################################################

_SHELL_CSS = """
<style>
body { margin: 0; }
.sr-shell { max-width: 760px; margin: 0 auto; padding: 16px 18px 48px; }
.sr-block-badge {
    position: fixed; top: 12px; right: 16px; z-index: 10;
    font-size: 11px; font-weight: 700; opacity: 0.4;
    letter-spacing: 0.06em; text-transform: uppercase;
}
.sr-reason {
    padding: 12px 16px; margin-bottom: 18px; border-radius: 10px;
    background: rgba(124,110,245,0.1); border: 1px solid rgba(124,110,245,0.22);
    font-size: 15px; line-height: 1.5;
}
.sr-btn {
    font-size: 14px; font-weight: 600; padding: 10px 22px; margin: 6px 8px 0 0;
    border: none; border-radius: 20px;
    background: linear-gradient(135deg, #7c6ef5, #6558e0);
    cursor: pointer; color: #fff;
    transition: opacity 0.18s, transform 0.12s;
    box-shadow: 0 2px 8px rgba(124,110,245,0.3);
    font-family: inherit;
}
.sr-btn:hover { opacity: 0.88; transform: translateY(-1px); }
.sr-btn:active { transform: translateY(0); }
.sr-btn.ghost {
    background: rgba(255,255,255,0.06); color: inherit;
    border: 1px solid rgba(255,255,255,0.12);
    box-shadow: none; font-size: 13px; padding: 7px 16px;
}
.sr-btn.ghost:hover { background: rgba(255,255,255,0.1); border-color: rgba(255,255,255,0.2); opacity: 1; }
.sr-card {
    border: 1px solid rgba(255,255,255,0.07); border-radius: 14px;
    padding: 20px 22px; margin-top: 4px;
    background: rgba(255,255,255,0.025);
    font-size: 14px; line-height: 1.5;
}
.sr-card h2 { margin: 0 0 10px; font-size: 18px; }
.sr-topic-title { font-size: 22px; font-weight: 800; margin: 0 0 14px; letter-spacing: -0.02em; }
.sr-scores { display: flex; gap: 28px; margin: 4px 0 20px; }
.sr-score-label {
    display: block; font-size: 11px; font-weight: 700;
    opacity: 0.45; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px;
}
.sr-score-val { font-size: 28px; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.sr-upnext { font-size: 14px; opacity: 0.65; margin-bottom: 16px; }
.sr-footer { margin-top: 24px; }
table.sr-summary { width: 100%; border-collapse: collapse; margin: 12px 0 20px; font-size: 14px; }
table.sr-summary th, table.sr-summary td {
    text-align: left; padding: 8px 10px;
    border-top: 1px solid rgba(255,255,255,0.07);
}
table.sr-summary th { opacity: 0.45; font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
table.sr-summary td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.sr-summary td.delta { opacity: 0.65; font-size: 13px; }
.sr-summary-actions { margin-top: 8px; }
</style>
"""


def _esc(text: str) -> str:
    return escape(text)


def _shell(reason: str, inner: str, footer: bool = True, block_label: str = "") -> str:
    parts = [_SHELL_CSS]
    if block_label:
        parts.append(f'<div class="sr-block-badge">{_esc(block_label)}</div>')
    parts.append('<div class="sr-shell">')
    if reason:
        parts.append(f'<div class="sr-reason">{_esc(reason)}</div>')
    parts.append(inner)
    if footer:
        parts.append(
            '<div class="sr-footer">'
            '<button class="sr-btn ghost" onclick=\'pycmd("sr:end")\'>'
            "End session</button></div>"
        )
    parts.append("</div>")
    return "\n".join(parts)


# Controller
############################################################


class SpeedrunController:
    """Orchestrates one Speedrun session across question and flashcard blocks.

    The question, transition and summary screens render inline in the main
    window (``mw.web``) via a custom ``speedrun`` main-window state, so the loop
    feels like a native part of Anki rather than a dialog. Flashcard blocks are
    delegated to Anki's own reviewer, and control returns to the loop afterward.
    """

    def __init__(self, mw: aqt.main.AnkiQt, deck_id: DeckId) -> None:
        self.mw = mw
        self.deck_id = deck_id
        self.speedrun_loop, self.performance_score = _modules()
        self.topic_decks = scope_topics(mw, deck_id)
        self.session = self.speedrun_loop.SpeedrunSession(list(self.topic_decks))

        self._ended = False
        self._served_any = False
        self._rng = random.Random()
        self._body = ""  # HTML for the screen currently shown in mw.web
        # Session structure: SESSION_BLOCKS blocks, then a summary screen.
        self._blocks_done = 0
        self._baseline: dict[str, dict[str, float | None]] = {}
        # Question IDs served in the current session; passed to the generation
        # trigger so it can count how many *unseen* questions remain per topic.
        self._seen_question_ids: set[str | int] = set()

        # Flashcard-block delegation state.
        self._fc_active = False
        self._fc_target = 0
        self._fc_count = 0
        self._current_plan: Any = None
        # Temporary filtered deck used to deliver a mixed flashcard block.
        self._filtered_did: DeckId | None = None
        # Per-question (concept_correct, answer_correct) results for the block
        # currently in progress, used for block-level routing.
        self._block_results: list[tuple[bool, bool]] = []
        # Temporary store for concept-grading results between srq:grade_concept
        # and srq:grade_answer (keyed by question id).
        self._pending_concept: dict[str | int, dict] = {}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._register_state()
        if not self.topic_decks:
            self._show_done("No MCAT topics were found under this deck.")
            return
        self._baseline = self._section_scores()
        self.serve_next()

    # -- main-window state ---------------------------------------------------

    def _register_state(self) -> None:
        """Install the custom ``speedrun`` main-window state handlers."""
        self.mw._speedrunState = self._enter_state  # type: ignore[attr-defined]
        self.mw._speedrunCleanup = self._exit_state  # type: ignore[attr-defined]

    def _unregister_state(self) -> None:
        for attr in ("_speedrunState", "_speedrunCleanup"):
            if hasattr(self.mw, attr):
                delattr(self.mw, attr)

    def _enter_state(self, _old_state: str) -> None:
        # Called by moveToState("speedrun"): paint the current screen.
        self._paint()

    def _exit_state(self, new_state: str) -> None:
        # Leaving our state for the reviewer (flashcard block) keeps the session
        # alive; leaving for anything else means the user navigated away, so end.
        if new_state == "review":
            return
        if not self._ended:
            self.end(navigated=True)

    def _paint(self) -> None:
        self.mw.web.set_bridge_command(self.on_bridge_cmd, self)
        self.mw.web.stdHtml(self._body, context=self)
        self.mw.bottomWeb.hide()
        self.mw.web.setFocus()

    def _render(self, body: str) -> None:
        """Show ``body`` inline in mw.web, entering the speedrun state if needed."""
        self._body = body
        if self.mw.state == SPEEDRUN_STATE:
            self._paint()
        else:
            self.mw.moveToState(SPEEDRUN_STATE)  # type: ignore[arg-type]

    def _stats(self) -> dict:
        return self.speedrun_loop.build_topic_stats(self.mw.col, list(self.topic_decks))

    def _block_label(self) -> str:
        return f"Block {min(self._blocks_done + 1, SESSION_BLOCKS)} of {SESSION_BLOCKS}"

    def _section_scores(self) -> dict[str, dict[str, float | None]]:
        """Per-section memory + performance score (None when below give-up rule)."""
        col = self.mw.col
        memory_score = self.speedrun_loop.memory_score
        mem_sections = memory_score.compute_sections(col)
        perf_by_code = {s.code: s for s in self.performance_score.compute_sections(col)}
        out: dict[str, dict[str, float | None]] = {}
        for s in mem_sections:
            perf = perf_by_code.get(s.code)
            out[s.code] = {
                "memory": s.average if s.has_score else None,
                "performance": perf.accuracy if perf and perf.has_score else None,
            }
        return out

    def serve_next(self) -> None:
        if self._ended:
            return
        stats = self._stats()
        plan = self.session.plan_block(stats)
        self._current_plan = plan
        if plan is None:
            self._show_done("That's all for now - great work.")
            return
        # When a question block is coming up, check whether any of the topics
        # involved are running low on approved questions and, if so, start
        # background AI generation so the pool stays stocked for future blocks.
        if plan.kind == "questions":
            self._maybe_trigger_generation(plan)
        if not self._served_any:
            # First block: drop straight in, no transition screen.
            self._served_any = True
            self._start_block(plan)
        else:
            self._show_transition(plan, stats)

    def _maybe_trigger_generation(self, plan: Any) -> None:
        """Fire background generation for topics with fewer than threshold questions.

        Runs the check and enqueues a daemon thread synchronously but returns
        immediately — the student session is never blocked.  All exceptions are
        swallowed so this can never crash the app.
        """
        try:
            from aqt.speedrun.auto_generator import maybe_trigger_generation
        except ModuleNotFoundError:
            repo_root = Path(aqt.__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from aqt.speedrun.auto_generator import maybe_trigger_generation
        except Exception:  # noqa: BLE001
            return  # module unavailable — silently skip

        try:
            seen = self._seen_question_ids
            if plan.topic:
                # Focused block: only the one in-scope topic matters.
                maybe_trigger_generation(plan.topic, seen_ids=seen)
            else:
                # Mixed block: check every in-scope topic.
                for topic in self.topic_decks:
                    maybe_trigger_generation(topic, seen_ids=seen)
        except Exception:  # noqa: BLE001
            pass  # never surface generation errors to the student

    def _start_block(self, plan: Any) -> None:
        self._block_results = []
        if plan.kind == "flashcards":
            self.start_flashcards()
        else:
            self._show_question_block(plan)

    def on_block_done(self) -> None:
        """Called after a question block finishes; advance the loop."""
        if self._ended:
            return
        BlockOutcome = self.speedrun_loop.BlockOutcome
        outcome = BlockOutcome("questions", list(self._block_results))
        self.session.after_block(self._stats(), outcome)
        self._advance_after_block()

    def _advance_after_block(self) -> None:
        """Count a completed block; end the session after SESSION_BLOCKS."""
        self._blocks_done += 1
        if self._blocks_done >= SESSION_BLOCKS:
            self._show_summary()
        else:
            self.serve_next()

    def _new_session(self) -> None:
        self.session = self.speedrun_loop.SpeedrunSession(list(self.topic_decks))
        self._blocks_done = 0
        self._served_any = False
        self._seen_question_ids = set()
        self._baseline = self._section_scores()
        self.serve_next()

    def end(self, navigated: bool = False) -> None:
        if self._ended:
            return
        self._ended = True
        self._detach_flashcard_hooks()
        self._cleanup_filtered_deck()
        self._unregister_state()
        self.mw.bottomWeb.show()
        if getattr(self.mw, "_speedrun_controller", None) is self:
            self.mw._speedrun_controller = None  # type: ignore[attr-defined]
        # If the user didn't navigate away themselves, return to the deck's
        # overview; otherwise their navigation already changed the state.
        if not navigated and self.mw.state == SPEEDRUN_STATE:
            self.mw.moveToState("overview")

    # -- rendering -----------------------------------------------------------

    @staticmethod
    def _fmt_pct(value: float | None) -> str:
        return "\u2014" if value is None else f"{round(value * 100)}%"

    def _show_transition(self, plan: Any, stats: dict) -> None:
        """Brief between-block screen: topic, scores, and what's coming next."""
        kind_word = "flashcards" if plan.kind == "flashcards" else "questions"
        topic = plan.topic
        title = topic if topic else "Mixed practice"
        up_next = (
            f"Up next: {topic} {kind_word}" if topic else f"Up next: Mixed {kind_word}"
        )

        scores = ""
        ts = stats.get(topic) if topic else None
        if ts is not None:
            scores = (
                '<div class="sr-scores">'
                '<div><span class="sr-score-label">Memory score</span>'
                f'<span class="sr-score-val">{self._fmt_pct(ts.memory)}</span></div>'
                '<div><span class="sr-score-label">Performance score</span>'
                f'<span class="sr-score-val">{self._fmt_pct(ts.performance)}</span>'
                "</div></div>"
            )

        inner = (
            '<div class="sr-card">'
            f'<div class="sr-topic-title">{_esc(title)}</div>'
            f"{scores}"
            f'<div class="sr-upnext">{_esc(up_next)}</div>'
            '<button class="sr-btn" onclick=\'pycmd("sr:continue")\'>Continue</button>'
            "</div>"
        )
        self._render(_shell("", inner, block_label=self._block_label()))

    def _get_api_key(self) -> str:
        """Return OPENAI_API_KEY, loading .env if needed."""
        import os
        from pathlib import Path as _Path

        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            env_path = _Path(__file__).parent.parent.parent.parent / ".env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("OPENAI_API_KEY="):
                        key = line.split("=", 1)[1].strip().strip("\"'")
                        if key:
                            os.environ["OPENAI_API_KEY"] = key
                        break
        return key

    def _show_question_block(self, plan: Any) -> None:
        # Record these IDs as seen before rendering so the generation trigger
        # (fired in serve_next just before this) has an accurate unseen count
        # for subsequent blocks.
        self._seen_question_ids.update(plan.question_ids)
        self._pending_concept.clear()
        questions = self.performance_score.client_questions_for_ids(plan.question_ids)
        ai_available = bool(self._get_api_key())
        inner = self.performance_score.render_question_block(
            questions, ai_available=ai_available
        )
        self._render(_shell(plan.reason, inner, block_label=self._block_label()))

    def _show_done(self, message: str) -> None:
        inner = (
            '<div class="sr-card">'
            f"<p>{_esc(message)}</p>"
            '<button class="sr-btn" onclick=\'pycmd("sr:close")\'>Close</button>'
            "</div>"
        )
        self._render(_shell("", inner, footer=False))

    @staticmethod
    def _fmt_delta(before: float | None, after: float | None) -> str:
        if before is None or after is None:
            return "&mdash;"
        change = round((after - before) * 100)
        arrow = "\u25b2" if change > 0 else "\u25bc" if change < 0 else ""
        return f"{arrow}{abs(change)}%" if change else "no change"

    def _show_summary(self) -> None:
        current = self._section_scores()
        memory_score = self.speedrun_loop.memory_score
        rows = ""
        for code in memory_score.SECTION_ORDER:
            base = self._baseline.get(code, {})
            now = current.get(code, {})
            mem, perf = now.get("memory"), now.get("performance")
            rows += (
                "<tr>"
                f"<td>{_esc(code)}</td>"
                f'<td class="num">{self._fmt_pct(mem)}</td>'
                f'<td class="num delta">{self._fmt_delta(base.get("memory"), mem)}</td>'
                f'<td class="num">{self._fmt_pct(perf)}</td>'
                f'<td class="num delta">'
                f"{self._fmt_delta(base.get('performance'), perf)}</td>"
                "</tr>"
            )
        inner = (
            '<div class="sr-card">'
            "<h2>Session complete</h2>"
            f"<p>You finished {SESSION_BLOCKS} blocks. Here's how your scores "
            "changed this session:</p>"
            '<table class="sr-summary">'
            '<tr><th>Section</th><th class="num">Memory</th>'
            '<th class="num">Change</th><th class="num">Performance</th>'
            '<th class="num">Change</th></tr>'
            f"{rows}"
            "</table>"
            '<div class="sr-summary-actions">'
            '<button class="sr-btn" onclick=\'pycmd("sr:new")\'>New session</button>'
            '<button class="sr-btn ghost" onclick=\'pycmd("sr:close")\'>Stop</button>'
            "</div>"
            "</div>"
        )
        self._render(_shell("", inner, footer=False))

    # -- flashcard delegation ------------------------------------------------

    def start_flashcards(self) -> None:
        if self._ended or self._current_plan is None:
            return
        if self._current_plan.topic is None:
            # Mixed flashcard block (general deck): review points-at-stake
            # weighted cards drawn from every in-scope topic.
            self._start_mixed_flashcards()
            return
        deck_id = self.topic_decks.get(self._current_plan.topic)
        if deck_id is None:
            # No flashcard deck for this topic (e.g. CARS, or topic not found).
            self._abort_remediation()
            return
        self._begin_reviewer(deck_id, self._current_plan.size)

    def _begin_reviewer(self, deck_id: DeckId, target: int) -> None:
        self._fc_active = True
        self._fc_target = target
        self._fc_count = 0
        self._attach_flashcard_hooks()

        self.mw.col.decks.select(deck_id)
        self.mw.col.startTimebox()
        self.mw.moveToState("review")

    def _select_mixed_cards(self) -> list[int]:
        """Weighted card ids across in-scope topics for a mixed flashcard block."""
        col = self.mw.col
        stats = self._stats()
        pools: dict[str, list[int]] = {}
        for topic, deck_id in self.topic_decks.items():
            if deck_id is None:
                # Questions-only topic (e.g. CARS) — no flashcard deck.
                continue
            if topic in self.session.exhausted_topics:
                continue
            deck = col.decks.get(deck_id)
            if not deck:
                continue
            cids = [
                int(c) for c in col.find_cards(f'deck:"{deck["name"]}" -is:suspended')
            ]
            if cids:
                self._rng.shuffle(cids)
                pools[topic] = cids
        return self.speedrun_loop.weighted_interleave(
            pools, stats, self._current_plan.size, rng=self._rng
        )

    def _start_mixed_flashcards(self) -> None:
        cids = self._select_mixed_cards()
        if not cids:
            self._show_done("No cards are available to review right now.")
            return
        filtered = self._build_filtered_deck(cids)
        if filtered is not None:
            self._filtered_did = filtered
            self._begin_reviewer(filtered, len(cids))
        else:
            # Fall back to reviewing the selected deck tree (still mixes topics,
            # but by Anki's own ordering rather than points-at-stake).
            self._begin_reviewer(self.deck_id, len(cids))

    def _build_filtered_deck(self, cids: list[int]) -> DeckId | None:
        """Create a temporary filtered deck holding exactly ``cids``."""
        try:
            col = self.mw.col
            deck = col.sched.get_or_create_filtered_deck(DeckId(0))
            deck.name = "Speedrun (mixed block)"
            config = deck.config
            config.reschedule = True
            del config.search_terms[:]
            term = config.search_terms.add()
            term.search = "cid:" + ",".join(str(c) for c in cids)
            term.limit = len(cids)
            term.order = 0  # type: ignore[assignment]
            out = col.sched.add_or_update_filtered_deck(deck)
            did = DeckId(out.id)
            col.sched.rebuild_filtered_deck(did)
            return did
        except Exception:
            return None

    def _cleanup_filtered_deck(self) -> None:
        if self._filtered_did is None:
            return
        try:
            self.mw.col.sched.empty_filtered_deck(self._filtered_did)
            self.mw.col.decks.remove([self._filtered_did])
        except Exception:
            pass
        self._filtered_did = None

    def _attach_flashcard_hooks(self) -> None:
        gui_hooks.reviewer_did_answer_card.append(self._on_reviewer_answer)
        gui_hooks.reviewer_will_end.append(self._on_reviewer_will_end)

    def _detach_flashcard_hooks(self) -> None:
        try:
            gui_hooks.reviewer_did_answer_card.remove(self._on_reviewer_answer)
        except ValueError:
            pass
        try:
            gui_hooks.reviewer_will_end.remove(self._on_reviewer_will_end)
        except ValueError:
            pass

    def _on_reviewer_answer(self, *_args: Any) -> None:
        if not self._fc_active:
            return
        self._fc_count += 1
        if self._fc_count >= self._fc_target:
            # Leave the reviewer once the current answer has been processed.
            self.mw.progress.single_shot(50, self._leave_reviewer, False)

    def _leave_reviewer(self) -> None:
        if not self._fc_active:
            return
        # Return to our inline state (rather than flashing the overview);
        # reviewer cleanup fires reviewer_will_end -> _finish_flashcards, which
        # repaints with the next block/transition.
        self._body = _shell("", '<div class="sr-card"><p>Loading&hellip;</p></div>')
        self.mw.moveToState(SPEEDRUN_STATE)  # type: ignore[arg-type]

    def _on_reviewer_will_end(self) -> None:
        # Fires both when we leave voluntarily and when cards run out early.
        if not self._fc_active:
            return
        self.mw.progress.single_shot(50, self._finish_flashcards, False)

    def _finish_flashcards(self) -> None:
        if not self._fc_active:
            return
        answered = self._fc_count
        self._fc_active = False
        self._detach_flashcard_hooks()
        was_mixed = self._filtered_did is not None
        self._cleanup_filtered_deck()

        if self._ended:
            return
        # If the user navigated away (e.g. clicked Decks) while the flashcard
        # block was running, respect that navigation instead of overriding it.
        if self.mw.state not in ("review", SPEEDRUN_STATE):
            self.end(navigated=True)
            return
        if answered == 0 and not was_mixed:
            # No cards were available for this topic; don't loop on it.
            self._abort_remediation()
            return
        BlockOutcome = self.speedrun_loop.BlockOutcome
        self.session.after_block(self._stats(), BlockOutcome("flashcards"))
        self._advance_after_block()

    def _abort_remediation(self) -> None:
        Mode = self.speedrun_loop.Mode
        topic = self._current_plan.topic if self._current_plan else None
        # Mark the topic exhausted so the loop won't keep picking it, then reset.
        self.session.mark_topic_exhausted(topic)
        self.session.mode = Mode.INTERLEAVED_DISCOVERY
        self.session.focus_topic = None
        self.session.focus_gap = None
        self.serve_next()

    # -- bridge --------------------------------------------------------------

    def on_bridge_cmd(self, cmd: str) -> Any:
        if cmd.startswith("srq:grade_concept:"):
            return self._grade_concept(cmd[len("srq:grade_concept:") :])
        if cmd.startswith("srq:grade_answer:"):
            return self._grade_answer_step(cmd[len("srq:grade_answer:") :])
        if cmd.startswith("srq:explain:"):
            return self._explain(cmd[len("srq:explain:") :])
        if cmd.startswith("srq:grade:"):
            return self._grade(cmd[len("srq:grade:") :])
        if cmd == "sr:continue":
            self._start_block(self._current_plan)
        elif cmd == "sr:new":
            self._new_session()
        elif cmd == "sr:block_done":
            self.on_block_done()
        elif cmd == "sr:end":
            self.end()
        elif cmd == "sr:close":
            self.end()
        return False

    def _grade_concept(self, encoded: str) -> Any:
        """Step 1: AI-grade the free-response concept answer and cache the result."""
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            qid = payload["id"]
            response = payload.get("response", "")
        except Exception as exc:
            return {"error": str(exc)}

        # Look up the question to get concept/rationale for grading.
        question = self.performance_score.questions_by_id().get(qid)
        if question is None:
            return {"ai_unavailable": True}

        result = self.performance_score.grade_concept_with_ai(
            question, response, self._get_api_key()
        )
        # Cache so grade_answer_step can read it without the client re-sending.
        self._pending_concept[qid] = result
        return result

    def _grade_answer_step(self, encoded: str) -> Any:
        """Step 2: Grade the MC answer and persist the full attempt to DB."""
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            qid = payload["id"]
            answer = payload.get("answer", "")
            # Server cache is the authority for concept grading results.
            # Use None sentinel so we can distinguish "graded wrong" from "never graded".
            pending = self._pending_concept.pop(qid, None)
            concept_was_graded = pending is not None
            concept_correct = bool((pending or {}).get("concept_correct", False))
            application_correct = bool((pending or {}).get("application_correct", False))
        except Exception as exc:
            return {"error": str(exc)}

        # Look up the question now so we always have fallback info even on error.
        question = self.performance_score.questions_by_id().get(qid)

        def _fallback(exc: Exception) -> dict:
            """Return enough info for the JS to show the correct answer/concept."""
            if question is None:
                return {"error": str(exc)}
            return {
                "error": str(exc),
                "fallback": True,
                "correct_answer": question.correct_answer,
                "correct_concept": question.concept,
                "rationale": question.rationale or question.concept,
            }

        try:
            res = self.performance_score.grade_answer_for_session(
                self.mw.col,
                qid,
                answer,
                concept_correct=concept_correct,
                application_correct=application_correct,
            )
        except Exception as exc:
            return _fallback(exc)

        if "error" in res:
            return _fallback(Exception(res["error"]))

        self._block_results.append(
            (bool(res["concept_correct"]), bool(res["answer_correct"]))
        )
        # Tell the JS whether concept grading actually ran so it can decide
        # whether to show the concept verdict.
        res["concept_was_graded"] = concept_was_graded
        return res

    def _explain(self, encoded: str) -> Any:
        """Step 3: Generate a wrong-answer explanation with GPT-4o."""
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            qid = payload["id"]
        except Exception as exc:
            return {"error": str(exc)}

        question = self.performance_score.questions_by_id().get(qid)
        if question is None:
            return {"error": f"unknown question id {qid}"}

        return self.performance_score.generate_explanation_with_ai(
            question,
            payload.get("chosen_answer", ""),
            bool(payload.get("concept_correct", False)),
            bool(payload.get("application_correct", False)),
            bool(payload.get("answer_correct", False)),
            self._get_api_key(),
        )

    def _grade(self, encoded: str) -> Any:
        """Legacy single-step grader (kept for CLI backward compat)."""
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            res = self.performance_score.grade_question(
                self.mw.col,
                payload["id"],
                payload.get("concept"),
                payload.get("answer"),
            )
        except Exception as exc:
            return {"error": str(exc)}
        if "error" not in res:
            self._block_results.append(
                (bool(res["concept_correct"]), bool(res["answer_correct"]))
            )
        return res


def _purge_orphaned_speedrun_decks(mw: aqt.main.AnkiQt) -> None:
    """Remove any leftover 'Speedrun (mixed block)' filtered decks.

    These can accumulate if Anki was quit mid-session before cleanup ran.
    """
    col = mw.col
    to_remove = [
        DeckId(int(d["id"]))
        for d in col.decks.all()
        if d.get("name", "").startswith("Speedrun (mixed block)")
        and d.get("dyn", 0)  # filtered decks have dyn=1
    ]
    for did in to_remove:
        try:
            col.sched.empty_filtered_deck(did)
            col.decks.remove([did])
        except Exception:
            pass


def _ensure_auto_sync(mw: aqt.main.AnkiQt) -> None:
    """Enable sync-on-open/close if the user has a sync account configured.

    This ensures that phone reviews are reflected on the desktop after the
    next app open, satisfying the offline-first sync requirement without any
    extra user setup step.
    """
    try:
        if mw.pm.sync_auth() is not None and not mw.pm.auto_syncing_enabled():
            mw.pm.profile["autoSync"] = True
            _log.info("Speedrun enabled auto-sync on open/close.")
    except Exception:  # noqa: BLE001
        pass


def _start_question_sync_pull() -> None:
    """Background-pull questions and performance records from Firestore.

    Spawns a daemon thread so app startup is never blocked.
    """
    import threading

    def _run() -> None:
        try:
            from aqt.speedrun.question_sync import (
                maybe_pull_into_local_cache,
                maybe_sync_performance,
            )
        except ModuleNotFoundError:
            repo_root = Path(aqt.__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from aqt.speedrun.question_sync import (
                maybe_pull_into_local_cache,
                maybe_sync_performance,
            )
        except Exception:  # noqa: BLE001
            return
        try:
            maybe_pull_into_local_cache()
        except Exception:  # noqa: BLE001
            pass
        try:
            mw = aqt.mw
            if mw and mw.col:
                maybe_sync_performance(mw.col)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_run, daemon=True, name="speedrun-sync-pull").start()


_PRACTICE_CSS = _SHELL_CSS + """
<style>
.pq-shell { max-width: 760px; margin: 0 auto; padding: 16px 18px 48px; }
.pq-progress-wrap { margin-bottom: 14px; }
.pq-progress-bar {
    height: 4px; border-radius: 2px; overflow: hidden;
    background: rgba(255,255,255,0.08); margin-bottom: 8px;
}
.pq-progress-fill {
    height: 100%; border-radius: 2px;
    background: linear-gradient(90deg, #7c6ef5, #9d8fff);
    transition: width 0.35s ease;
}
.pq-progress-text { opacity: 0.4; font-size: 12px; }
.pq-score { margin-left: 8px; font-size: 12px; font-weight: 700; opacity: 0.7; }
/* Config screen */
.pq-config-label {
    font-size: 11px; font-weight: 700; opacity: 0.45;
    text-transform: uppercase; letter-spacing: 0.06em;
    display: block; margin: 18px 0 8px;
}
.pq-chips { display: flex; flex-wrap: wrap; gap: 8px; }
.pq-chip {
    padding: 7px 16px; border-radius: 20px; font-size: 13px; font-weight: 600;
    border: 1px solid rgba(255,255,255,0.15); background: rgba(255,255,255,0.04);
    cursor: pointer; color: inherit; font-family: inherit;
    transition: border-color 0.15s, background 0.15s;
}
.pq-chip:hover { background: rgba(124,110,245,0.1); border-color: rgba(124,110,245,0.4); }
.pq-chip.active {
    background: rgba(124,110,245,0.2); border-color: rgba(124,110,245,0.7);
    color: #a89fff;
}
</style>
"""

_PRACTICE_CONFIG_JS = """
<script>
(function() {
  var selCount = %%DEFAULT_COUNT%%;
  var selSection = "All";
  var counts = [10, 20, 30, 40, 60];
  var sections = %%SECTIONS%%;

  function setCount(n) {
    selCount = n;
    counts.forEach(function(c) {
      var el = document.getElementById("cnt-"+c);
      if (el) el.classList.toggle("active", c === n);
    });
  }
  function setSection(s) {
    selSection = s;
    sections.forEach(function(sec) {
      var el = document.getElementById("sec-"+sec.replace("/","-"));
      if (el) el.classList.toggle("active", sec === s);
    });
  }
  window.setCount = setCount;
  window.setSection = setSection;
  window.startQuiz = function() {
    var encoded = encodeURIComponent(JSON.stringify({count: selCount, section: selSection}));
    pycmd("pq:start:" + encoded);
  };
})();
</script>
"""

_PRACTICE_JS = """
<script>
(function() {
  var questions = %%QUESTIONS%%;
  var idx = 0;
  var correct = 0;
  var total = questions.length;
  var LETTERS = ["A","B","C","D","E","F"];

  function esc(s) {
    return String(s)
      .replace(/&/g,"&amp;").replace(/</g,"&lt;")
      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }

  function render() {
    if (idx >= total) { finish(); return; }
    var q = questions[idx];
    var pct = Math.round(idx / total * 100);

    var passageHtml = "";
    if (q.passage) {
      passageHtml = '<details style="margin:8px 0 4px">'
        + '<summary style="cursor:pointer;opacity:0.5;font-size:12px;margin-bottom:4px">Show passage</summary>'
        + '<div style="font-size:13px;line-height:1.6;opacity:0.8;border-left:3px solid rgba(124,110,245,.5);padding-left:12px;margin-top:8px">'
        + esc(q.passage) + '</div></details>';
    }

    var choicesHtml = q.choices.map(function(c, i) {
      return '<button class="mcat-choice" id="ch'+i+'" onclick="pick('+i+')">'
        + '<span class="letter">'+LETTERS[i]+'</span>' + esc(c) + '</button>';
    }).join("");

    document.getElementById("pq-root").innerHTML =
      '<div class="pq-progress-wrap">'
      + '<div class="pq-progress-bar"><div class="pq-progress-fill" style="width:'+pct+'%"></div></div>'
      + '<span class="pq-progress-text">Question '+(idx+1)+' of '+total+'</span>'
      + '<span class="pq-score">'+correct+' correct</span>'
      + '</div>'
      + '<div class="mcat-quiz-card">'
      + '<div class="mcat-quiz-topic">'+esc(q.topic)+'</div>'
      + passageHtml
      + '<div class="mcat-quiz-question">'+esc(q.question)+'</div>'
      + '<div class="mcat-quiz-choices">'+choicesHtml+'</div>'
      + '<div id="pq-feedback"></div>'
      + '</div>';
  }

  window.pick = function(i) {
    var q = questions[idx];
    var choices = document.querySelectorAll(".mcat-choice");
    choices.forEach(function(b) { b.disabled = true; });
    var encoded = encodeURIComponent(JSON.stringify({id: q.id, answer: q.choices[i]}));
    pycmd("pq:grade:" + encoded, function(res) {
      var wasCorrect = res.answer_correct;
      if (wasCorrect) correct++;
      choices[i].classList.add(wasCorrect ? "correct" : "incorrect");
      if (!wasCorrect) {
        choices.forEach(function(b, j) {
          if (q.choices[j] === res.correct_answer) b.classList.add("correct");
        });
      }
      var rationale = res.rationale || res.correct_concept || "";
      document.getElementById("pq-feedback").innerHTML =
        '<div class="mcat-quiz-feedback">'
        + '<div class="mcat-verdict-row">'
        + '<span class="verdict '+(wasCorrect?"correct":"incorrect")+'">'
        + (wasCorrect ? "Correct" : "Incorrect") + '</span>'
        + (rationale ? '<span class="note">'+esc(rationale)+'</span>' : '')
        + '</div>'
        + '<div class="mcat-quiz-actions" style="margin-top:14px">'
        + (idx + 1 < total
            ? '<button class="sr-btn" onclick="next()">Next \u2192</button>'
            : '<button class="sr-btn" onclick="next()">See results</button>')
        + '</div></div>';
    });
  };

  window.next = function() { idx++; render(); };

  function finish() {
    var pct = total > 0 ? Math.round(correct / total * 100) : 0;
    var color = pct >= 70 ? "#4ade80" : pct >= 50 ? "#fbbf24" : "#f87171";
    document.getElementById("pq-root").innerHTML =
      '<div class="mcat-quiz-card">'
      + '<h2 style="margin:0 0 6px">Practice Quiz Complete</h2>'
      + '<p style="font-size:32px;font-weight:800;margin:10px 0;color:'+color+'">'
      + correct + ' / ' + total + '</p>'
      + '<p style="opacity:0.6;font-size:14px;margin:0 0 20px">'+pct+'% correct</p>'
      + '<div style="display:flex;gap:8px;flex-wrap:wrap">'
      + '<button class="sr-btn" onclick=\'pycmd("pq:config")\'>New quiz</button>'
      + '<button class="sr-btn ghost" onclick=\'pycmd("pq:done")\'>Done</button>'
      + '</div></div>';
  }

  render();
})();
</script>
"""

# Topics belonging to each MCAT section (used to filter questions).
_SECTION_TOPICS: dict[str, list[str]] = {
    "B/B": ["Biology", "Biochemistry", "Essential-Equations"],
    "C/P": ["General-Chemistry", "Organic-Chemistry", "Physics-and-Math"],
    "P/S": ["Behavioral"],
    "CARS": ["CARS"],
}
_ALL_SECTIONS = ["All"] + list(_SECTION_TOPICS)


class PracticeQuizController:
    """Configurable interleaved MC quiz — no concept step, no flashcard blocks."""

    def __init__(self, mw: aqt.main.AnkiQt, deck_id: DeckId) -> None:
        self.mw = mw
        self.deck_id = deck_id
        self.speedrun_loop, self.performance_score = _modules()
        self.topic_decks = scope_topics(mw, deck_id)
        self._ended = False
        self._question_ids: list[str | int] = []
        self._count = PRACTICE_QUIZ_SIZE
        self._section = "All"
        self._body = ""

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._register_state()
        if not self.topic_decks:
            self._show_error("No MCAT topics found.")
            return
        self._show_config()

    def _register_state(self) -> None:
        self.mw._speedrunState = self._enter_state  # type: ignore[attr-defined]
        self.mw._speedrunCleanup = self._exit_state  # type: ignore[attr-defined]

    def _enter_state(self, _old: str) -> None:
        self._paint_current()

    def _exit_state(self, new_state: str) -> None:
        if new_state != SPEEDRUN_STATE and not self._ended:
            self.end(navigated=True)

    def _paint_current(self) -> None:
        """Repaint the current body into mw.web (mirrors SpeedrunController._paint)."""
        self.mw.web.set_bridge_command(self.on_bridge_cmd, self)
        self.mw.web.stdHtml(self._body, context=self)
        self.mw.bottomWeb.hide()
        self.mw.web.setFocus()

    def _paint(self, body: str) -> None:
        """Set a new body and render it, transitioning to speedrun state if needed."""
        self._body = body
        if self.mw.state == SPEEDRUN_STATE:
            self._paint_current()
        else:
            self.mw.moveToState(SPEEDRUN_STATE)  # type: ignore[arg-type]

    # -- config screen -------------------------------------------------------

    def _show_config(self) -> None:
        import json

        count_chips = "".join(
            f'<button class="pq-chip{" active" if n == self._count else ""}" '
            f'id="cnt-{n}" onclick="setCount({n})">{n}</button>'
            for n in [10, 20, 30, 40, 60]
        )
        section_chips = "".join(
            f'<button class="pq-chip{" active" if s == self._section else ""}" '
            f'id="sec-{s.replace("/","-")}" onclick="setSection(\'{s}\')">{s}</button>'
            for s in _ALL_SECTIONS
        )
        inner = (
            '<div class="sr-card">'
            "<h2 style='margin:0 0 4px'>Practice Quiz</h2>"
            "<p style='opacity:0.6;font-size:13px;margin:0 0 4px'>"
            "Questions are drawn adaptively based on your weakest areas.</p>"
            f'<span class="pq-config-label">Number of questions</span>'
            f'<div class="pq-chips">{count_chips}</div>'
            f'<span class="pq-config-label">Subject</span>'
            f'<div class="pq-chips">{section_chips}</div>'
            '<div style="margin-top:22px;display:flex;gap:8px">'
            '<button class="sr-btn" onclick="startQuiz()">Start quiz</button>'
            '<button class="sr-btn ghost" onclick=\'pycmd("pq:home")\'>&#8592; Back</button>'
            "</div></div>"
        )
        sections_json = json.dumps(_ALL_SECTIONS)
        js = (
            _PRACTICE_CONFIG_JS
            .replace("%%DEFAULT_COUNT%%", str(self._count))
            .replace("%%SECTIONS%%", sections_json)
        )
        self._paint(_PRACTICE_CSS + '<div class="sr-shell">' + inner + "</div>" + js)

    # -- quiz ----------------------------------------------------------------

    def _pick_questions(self) -> None:
        import random as _random

        # Restrict to topics in the chosen section.
        if self._section == "All":
            in_scope = dict(self.topic_decks)
        else:
            wanted = set(_SECTION_TOPICS.get(self._section, []))
            in_scope = {t: d for t, d in self.topic_decks.items() if t in wanted}

        stats = self.speedrun_loop.build_topic_stats(self.mw.col, list(in_scope))
        questions_by_topic = self.speedrun_loop._questions_by_topic(list(in_scope))

        ids = self.speedrun_loop.select_interleaved_questions(
            stats, questions_by_topic, self._count
        )
        # Fall back to random if no eligible topics yet (early in studying).
        if not ids:
            all_ids: list[str | int] = []
            for id_list in questions_by_topic.values():
                all_ids.extend(id_list)
            _random.shuffle(all_ids)
            ids = all_ids[: self._count]
        self._question_ids = ids

        # Fire background generation for any topics running low on questions,
        # so the pool stays stocked for future quizzes without blocking the UI.
        self._trigger_generation(list(in_scope), set(ids))

    def _trigger_generation(
        self, topics: list[str], seen_ids: set[str | int]
    ) -> None:
        """Start background AI generation for topics below the question threshold."""
        try:
            from aqt.speedrun.auto_generator import maybe_trigger_generation
        except ModuleNotFoundError:
            repo_root = Path(aqt.__file__).resolve().parents[2]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            try:
                from aqt.speedrun.auto_generator import maybe_trigger_generation
            except Exception:  # noqa: BLE001
                return
        except Exception:  # noqa: BLE001
            return

        for topic in topics:
            try:
                maybe_trigger_generation(topic, seen_ids=seen_ids)
            except Exception:  # noqa: BLE001
                pass

    def _render_quiz(self) -> None:
        import json

        questions = self.performance_score.client_questions_for_ids(self._question_ids)
        if not questions:
            self._show_error(
                f"No practice questions available for {self._section}."
                if self._section != "All"
                else "No practice questions available yet."
            )
            return
        questions_json = json.dumps(questions).replace("<", "\\u003c")
        js = _PRACTICE_JS.replace("%%QUESTIONS%%", questions_json)
        self._paint(
            _PRACTICE_CSS
            + '<div class="pq-shell"><div id="pq-root"></div></div>'
            + js
        )

    # -- bridge --------------------------------------------------------------

    def on_bridge_cmd(self, cmd: str) -> Any:
        if cmd.startswith("pq:grade:"):
            return self._grade(cmd[len("pq:grade:"):])
        if cmd.startswith("pq:start:"):
            return self._handle_start(cmd[len("pq:start:"):])
        if cmd == "pq:config":
            self._show_config()
        elif cmd == "pq:home":
            self._go_home()
        elif cmd in ("pq:done", "sr:end"):
            self.end()
        return False

    def _handle_start(self, encoded: str) -> None:
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            self._count = int(payload.get("count", PRACTICE_QUIZ_SIZE))
            self._section = str(payload.get("section", "All"))
        except Exception:  # noqa: BLE001
            pass
        self._pick_questions()
        self._render_quiz()

    def _grade(self, encoded: str) -> Any:
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            res = self.performance_score.grade_answer_for_session(
                self.mw.col,
                payload["id"],
                payload.get("answer", ""),
                concept_correct=False,
                application_correct=False,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "answer_correct": False}
        return res

    def _show_error(self, msg: str) -> None:
        inner = (
            '<div class="sr-card">'
            f"<p>{_esc(msg)}</p>"
            '<button class="sr-btn ghost" onclick=\'pycmd("pq:config")\'>Back</button>'
            "</div>"
        )
        self._paint(
            _PRACTICE_CSS + '<div class="sr-shell">' + inner + "</div>"
        )

    def _go_home(self) -> None:
        self.end(navigated=True)
        try:
            from aqt.speedrun.home import McatHomeController

            ctrl = getattr(self.mw, "_mcat_home_controller", None)
            if ctrl is None:
                ctrl = McatHomeController(self.mw)
                self.mw._mcat_home_controller = ctrl  # type: ignore[attr-defined]
            ctrl.show()
        except Exception:  # noqa: BLE001
            self.mw.moveToState("deckBrowser")

    def end(self, navigated: bool = False) -> None:
        if self._ended:
            return
        self._ended = True
        for attr in ("_speedrunState", "_speedrunCleanup"):
            if hasattr(self.mw, attr):
                delattr(self.mw, attr)
        if getattr(self.mw, "_practice_quiz_controller", None) is self:
            self.mw._practice_quiz_controller = None  # type: ignore[attr-defined]
        self.mw.bottomWeb.show()
        if not navigated and self.mw.state == SPEEDRUN_STATE:
            self.mw.moveToState("overview")


def start_practice_quiz(mw: aqt.main.AnkiQt) -> None:
    """Open the practice quiz config screen for the current MCAT deck."""
    deck = mw.col.decks.current()
    if deck.get("name", "") != MCAT_ROOT:
        return
    existing = getattr(mw, "_practice_quiz_controller", None)
    if existing is not None:
        existing.end()
    controller = PracticeQuizController(mw, DeckId(int(deck["id"])))
    mw._practice_quiz_controller = controller  # type: ignore[attr-defined]
    controller.start()




def maybe_start(mw: aqt.main.AnkiQt, deck: dict) -> bool:
    """Launch the Speedrun loop only for the top-level MCAT Study Blocks deck.

    Studying an individual subdeck falls through to standard Anki review.
    """
    if deck.get("name", "") != MCAT_ROOT:
        return False
    _ensure_auto_sync(mw)
    _start_question_sync_pull()
    _purge_orphaned_speedrun_decks(mw)
    existing = getattr(mw, "_speedrun_controller", None)
    if existing is not None:
        existing.end()
    controller = SpeedrunController(mw, DeckId(int(deck["id"])))
    mw._speedrun_controller = controller  # type: ignore[attr-defined]
    controller.start()
    return True
