# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""MCAT home screen — the default landing page for Speedrun.

Replaces the deck browser as the first thing students see when they open the
app.  Shows two primary actions (Adaptive Session, Practice Quiz) and a live
per-section score snapshot.  The deck browser remains accessible via the
"Decks" toolbar link.

The home screen runs as the custom main-window state ``"mcatHome"``, using the
same dynamic-state registration pattern as ``SPEEDRUN_STATE`` in driver.py.
"""

from __future__ import annotations

import datetime
from html import escape
from typing import TYPE_CHECKING, Any

import aqt

if TYPE_CHECKING:
    import aqt.main

MCAT_HOME_STATE: str = "mcatHome"
MCAT_ROOT = "MCAT Study Blocks"

_HOME_CSS = """
<style>
* { box-sizing: border-box; }
body {
    margin: 0; padding: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
#mcat-home {
    max-width: 700px;
    margin: 0 auto;
    padding: 3em 1.5em 4em;
}
/* Greeting */
#mcat-greeting {
    margin-bottom: 2em;
}
#mcat-salutation {
    display: block;
    font-size: 2rem;
    font-weight: 800;
    letter-spacing: -0.03em;
    line-height: 1.1;
}
#mcat-tagline {
    display: block;
    font-size: 0.95rem;
    opacity: 0.45;
    margin-top: 0.4em;
    font-weight: 500;
}
/* Action buttons */
#mcat-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 2em;
}
.mcat-action-btn {
    display: block;
    width: 100%;
    padding: 18px 20px;
    border-radius: 14px;
    border: none;
    font-size: 15px;
    font-weight: 700;
    cursor: pointer;
    font-family: inherit;
    text-align: left;
    line-height: 1.3;
    transition: opacity 0.18s, transform 0.12s;
}
.mcat-action-btn:hover { opacity: 0.88; transform: translateY(-1px); }
.mcat-action-btn:active { transform: translateY(0); }
.mcat-action-btn.primary {
    background: linear-gradient(135deg, #7c6ef5, #6558e0);
    color: #fff;
    box-shadow: 0 4px 16px rgba(124,110,245,0.35);
}
.mcat-action-btn.secondary {
    background: rgba(124,110,245,0.12);
    color: inherit;
    border: 1px solid rgba(124,110,245,0.3);
}
.mcat-action-label { display: block; font-size: 11px; font-weight: 700;
    opacity: 0.6; text-transform: uppercase; letter-spacing: 0.06em;
    margin-bottom: 4px; }
/* Score table */
#mcat-scores {
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    background: rgba(255,255,255,0.025);
    overflow: hidden;
    margin-bottom: 1.5em;
}
#mcat-scores-title {
    font-size: 11px; font-weight: 700; opacity: 0.4;
    text-transform: uppercase; letter-spacing: 0.06em;
    padding: 12px 16px 8px;
}
table.mcat-score-table {
    width: 100%; border-collapse: collapse; font-size: 13px;
}
table.mcat-score-table th {
    padding: 0 16px 6px;
    text-align: left; font-size: 10px; font-weight: 700;
    opacity: 0.35; text-transform: uppercase; letter-spacing: 0.06em;
}
table.mcat-score-table th.num { text-align: right; }
table.mcat-score-table td {
    padding: 8px 16px;
    border-top: 1px solid rgba(255,255,255,0.05);
}
table.mcat-score-table td.num {
    text-align: right; font-variant-numeric: tabular-nums;
}
.pct-bar-wrap {
    width: 80px; display: inline-block; vertical-align: middle;
    background: rgba(255,255,255,0.07); border-radius: 3px;
    height: 4px; margin-left: 8px;
}
.pct-bar-fill {
    height: 100%; border-radius: 3px;
    background: linear-gradient(90deg,#7c6ef5,#9d8fff);
}
.score-na { opacity: 0.3; }
/* Deck link */
#mcat-deck-link {
    text-align: right;
}
#mcat-deck-link button {
    background: none;
    border: none;
    font-size: 13px;
    opacity: 0.45;
    cursor: pointer;
    font-family: inherit;
    color: inherit;
    padding: 4px 0;
    transition: opacity 0.15s;
}
#mcat-deck-link button:hover { opacity: 0.85; }
</style>
"""


def _esc(s: str) -> str:
    return escape(str(s))


def _greeting() -> str:
    hour = datetime.datetime.now().hour
    if hour < 12:
        return "Good morning!"
    if hour < 17:
        return "Good afternoon!"
    return "Good evening!"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return '<span class="score-na">&mdash;</span>'
    pct = round(value * 100)
    bar = (
        f'<span class="pct-bar-wrap">'
        f'<span class="pct-bar-fill" style="width:{pct}%"></span>'
        f"</span>"
    )
    return f"{pct}%{bar}"


def _render_scores(mw: aqt.main.AnkiQt) -> str:
    """Build a compact per-section memory + performance table."""
    try:
        from aqt.speedrun import memory_score as ms
        from aqt.speedrun import performance_score as ps
    except Exception:  # noqa: BLE001
        return ""

    try:
        mem_sections = ms.compute_sections(mw.col)
        perf_sections = {s.code: s for s in ps.compute_sections(mw.col)}
    except Exception:  # noqa: BLE001
        return ""

    rows = ""
    for s in mem_sections:
        mem = s.average if s.has_score else None
        perf_s = perf_sections.get(s.code)
        perf = perf_s.accuracy if perf_s and perf_s.has_score else None
        rows += (
            f"<tr>"
            f"<td>{_esc(s.code)}</td>"
            f'<td class="num">{_fmt_pct(mem)}</td>'
            f'<td class="num">{_fmt_pct(perf)}</td>'
            f"</tr>"
        )

    # CARS — no memory score, only performance
    cars = perf_sections.get("CARS")
    cars_perf = cars.accuracy if cars and cars.has_score else None
    rows += (
        f"<tr>"
        f"<td>CARS</td>"
        f'<td class="num"><span class="score-na">&mdash;</span></td>'
        f'<td class="num">{_fmt_pct(cars_perf)}</td>'
        f"</tr>"
    )

    return (
        '<div id="mcat-scores">'
        '<div id="mcat-scores-title">Your scores</div>'
        '<table class="mcat-score-table">'
        "<tr>"
        "<th>Section</th>"
        '<th class="num">Memory</th>'
        '<th class="num">Performance</th>'
        "</tr>"
        f"{rows}"
        "</table></div>"
    )


def _build_html(mw: aqt.main.AnkiQt) -> str:
    scores_html = _render_scores(mw)
    return (
        _HOME_CSS
        + '<div id="mcat-home">'
        + '<div id="mcat-greeting">'
        + f'<span id="mcat-salutation">{_greeting()}</span>'
        + '<span id="mcat-tagline">Ready to study?</span>'
        + "</div>"
        + '<div id="mcat-actions">'
        + '<button class="mcat-action-btn primary" onclick=\'pycmd("mh:start")\'>'
        + '<span class="mcat-action-label">Adaptive</span>'
        + "Start Study Session"
        + "</button>"
        + '<button class="mcat-action-btn secondary" onclick=\'pycmd("mh:quiz")\'>'
        + '<span class="mcat-action-label">Practice</span>'
        + "Practice Quiz"
        + "</button>"
        + "</div>"
        + scores_html
        + '<div id="mcat-deck-link">'
        + '<button onclick=\'pycmd("mh:decks")\'>View decks &rsaquo;</button>'
        + "</div>"
        + "</div>"
    )


class McatHomeController:
    """Renders and manages the MCAT home screen as a custom main-window state."""

    def __init__(self, mw: aqt.main.AnkiQt) -> None:
        self.mw = mw
        self._body = ""

    # -- state registration --------------------------------------------------

    def register(self) -> None:
        self.mw._mcatHomeState = self._enter_state  # type: ignore[attr-defined]
        self.mw._mcatHomeCleanup = self._exit_state  # type: ignore[attr-defined]

    def unregister(self) -> None:
        for attr in ("_mcatHomeState", "_mcatHomeCleanup"):
            if hasattr(self.mw, attr):
                delattr(self.mw, attr)

    # -- state callbacks -----------------------------------------------------

    def _enter_state(self, _old: str) -> None:
        self._body = _build_html(self.mw)
        self.mw.web.set_bridge_command(self._on_bridge, self)
        self.mw.web.stdHtml(self._body, context=self)
        self.mw.bottomWeb.hide()
        self.mw.web.setFocus()

    def _exit_state(self, _new: str) -> None:
        pass  # nothing to clean up

    # -- bridge --------------------------------------------------------------

    def _on_bridge(self, cmd: str) -> Any:
        if cmd == "mh:start":
            self._launch_speedrun()
        elif cmd == "mh:quiz":
            self._launch_practice_quiz()
        elif cmd == "mh:decks":
            self.mw.moveToState("deckBrowser")
        return False

    # -- actions -------------------------------------------------------------

    def _launch_speedrun(self) -> None:
        from aqt.speedrun.driver import MCAT_ROOT, maybe_start

        deck = self.mw.col.decks.by_name(MCAT_ROOT)
        if not deck:
            return
        self.mw.col.decks.select(deck["id"])
        maybe_start(self.mw, deck)

    def _launch_practice_quiz(self) -> None:
        from aqt.speedrun.driver import start_practice_quiz

        deck = self.mw.col.decks.by_name(MCAT_ROOT)
        if not deck:
            return
        self.mw.col.decks.select(deck["id"])
        start_practice_quiz(self.mw)

    # -- public entry point --------------------------------------------------

    def show(self) -> None:
        """Navigate to the MCAT home state."""
        self.register()
        self.mw.moveToState(MCAT_HOME_STATE)  # type: ignore[arg-type]


# toolbar.py and main.py call McatHomeController directly — no module-level hooks needed.
