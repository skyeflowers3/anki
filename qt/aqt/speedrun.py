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

import random
import sys
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aqt
from anki.decks import DeckId
from aqt import gui_hooks
from aqt.qt import *

if TYPE_CHECKING:
    import aqt.main


MCAT_ROOT = "AnKing-MCAT"

# A session is a fixed number of blocks; after the last one a summary is shown.
SESSION_BLOCKS = 5

# Custom main-window state, so the loop's screens render inline in mw.web (the
# same web view the reviewer uses) instead of a separate dialog window. Typed as
# a plain str since it is not one of Anki's built-in MainWindowState literals.
SPEEDRUN_STATE: str = "speedrun"


def _modules():
    """Import the shared speedrun logic modules (see stats.py for rationale)."""
    try:
        from speedrun import performance_score, speedrun_loop
    except ModuleNotFoundError:
        repo_root = Path(aqt.__file__).resolve().parents[2]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from speedrun import performance_score, speedrun_loop

    return speedrun_loop, performance_score


def scope_topics(mw: aqt.main.AnkiQt, deck_id: DeckId) -> dict[str, DeckId]:
    """Map each in-scope topic (subdeck) to a deck id, within the selection.

    Topics are the AnKing-MCAT subdecks known to the loop (Behavioral,
    Biochemistry, ...). Reviewing a topic's deck naturally includes any of its
    child decks.
    """
    speedrun_loop, _ = _modules()
    known = speedrun_loop.POINTS_WEIGHTS
    root = mw.col.decks.get(deck_id)
    if not root:
        return {}
    root_name = root["name"]

    topics: dict[str, DeckId] = {}
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
    return topics


# Rendering
############################################################

_SHELL_CSS = """
<style>
body { margin: 0; }
.sr-shell { max-width: 760px; margin: 0 auto; padding: 16px 18px 40px; }
.sr-block-badge {
    position: fixed; top: 10px; right: 14px; z-index: 10;
    font-size: 12px; font-weight: 600; opacity: 0.6;
    letter-spacing: 0.03em;
}
.sr-reason {
    padding: 12px 16px; margin-bottom: 18px; border-radius: 8px;
    background: rgba(125,125,255,0.12); font-size: 16px; line-height: 1.4;
}
.sr-btn {
    font-size: 15px; font-weight: 600; padding: 11px 22px; margin: 6px 8px 0 0;
    border: 1px solid rgba(128,128,128,0.4); border-radius: 6px;
    background: rgba(125,125,255,0.16); cursor: pointer; color: inherit;
}
.sr-btn:hover { background: rgba(125,125,255,0.28); }
.sr-btn.ghost { background: transparent; opacity: 0.7; font-size: 13px; padding: 7px 14px; }
.sr-card {
    border: 1px solid rgba(128,128,128,0.3); border-radius: 8px;
    padding: 20px 22px; margin-top: 4px; font-size: 14px; line-height: 1.5;
}
.sr-card h2 { margin: 0 0 8px; font-size: 18px; }
.sr-topic-title { font-size: 20px; font-weight: 700; margin: 0 0 14px; }
.sr-scores { display: flex; gap: 28px; margin: 4px 0 20px; }
.sr-score-label {
    display: block; font-size: 12px; opacity: 0.65; text-transform: uppercase;
    letter-spacing: 0.04em; margin-bottom: 2px;
}
.sr-score-val { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.sr-upnext { font-size: 15px; opacity: 0.85; margin-bottom: 16px; }
.sr-footer { margin-top: 24px; }
table.sr-summary { width: 100%; border-collapse: collapse; margin: 12px 0 20px; font-size: 14px; }
table.sr-summary th, table.sr-summary td {
    text-align: left; padding: 7px 8px;
    border-top: 1px solid rgba(128,128,128,0.2);
}
table.sr-summary th { opacity: 0.6; font-weight: 600; }
table.sr-summary td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.sr-summary td.delta { opacity: 0.8; font-size: 13px; }
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
        if not self._served_any:
            # First block: drop straight in, no transition screen.
            self._served_any = True
            self._start_block(plan)
        else:
            self._show_transition(plan, stats)

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

    def _show_question_block(self, plan: Any) -> None:
        questions = self.performance_score.client_questions_for_ids(plan.question_ids)
        inner = self.performance_score.render_question_block(questions)
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
            # Nothing to review; skip remediation to avoid looping.
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

    def _grade(self, encoded: str) -> Any:
        import json
        from urllib.parse import unquote

        try:
            payload = json.loads(unquote(encoded))
            res = self.performance_score.grade_question(
                self.mw.col,
                int(payload["id"]),
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


def maybe_start(mw: aqt.main.AnkiQt, deck: dict) -> bool:
    """Launch the Speedrun loop only for the top-level AnKing-MCAT deck.

    Studying an individual subdeck falls through to standard Anki review.
    """
    if deck.get("name", "") != MCAT_ROOT:
        return False
    existing = getattr(mw, "_speedrun_controller", None)
    if existing is not None:
        existing.end()
    controller = SpeedrunController(mw, DeckId(int(deck["id"])))
    mw._speedrun_controller = controller  # type: ignore[attr-defined]
    controller.start()
    return True
