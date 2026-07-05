"""MCAT content-area coverage map for Speedrun.

Checks whether all required MCAT content areas are present in the current
collection and question bank, then reports a coverage percentage and the list
of covered/missing areas.

Coverage rules
--------------
Required areas (6):
    Behavioral Sciences, Biology, Biochemistry, General Chemistry,
    Organic Chemistry, Physics and Math, CARS.
    A flashcard-backed area is covered when its AnKing-MCAT subdeck exists in
    the collection AND contains at least one card.
    CARS is covered when at least one CARS question exists in the question bank.

Recommended areas (not required for the readiness score):
    Essential Equations — a supplemental deck that strengthens B/B recall.
    Missing it does not block the readiness score, but a recommendation is
    shown on the Readiness tab.

This module is intentionally read-only and does not touch quiz, deck, or
authentication logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

# ---------------------------------------------------------------------------
# Static definition of required areas
# ---------------------------------------------------------------------------

# Human-readable name → subdeck path component (or None for question-only areas).
# These must all be covered for the readiness score to show.
REQUIRED_AREAS: list[tuple[str, str | None]] = [
    ("Behavioral Sciences", "Behavioral"),
    ("Biology", "Biology"),
    ("Biochemistry", "Biochemistry"),
    ("General Chemistry", "General-Chemistry"),
    ("Organic Chemistry", "Organic-Chemistry"),
    ("Physics and Math", "Physics-and-Math"),
    ("CARS", None),  # covered via question bank, not flashcard deck
]

# Recommended areas: shown in the coverage tab and surfaced as a tip on the
# readiness tab, but missing ones do NOT block the readiness score.
RECOMMENDED_AREAS: list[tuple[str, str | None]] = [
    ("Essential Equations", "Essential-Equations"),
]

_QUESTIONS_PATH = Path(__file__).resolve().parent / "questions.json"
_GENERATED_PATH = Path(__file__).resolve().parent / "generated_questions.json"

# Internal Anki deck-name separator.
_DECK_SEP = "\x1f"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AreaStatus:
    """Coverage status for one MCAT content area."""

    name: str
    covered: bool
    note: str = ""       # short explanation of how it is covered (or why not)
    recommended: bool = False  # True = nice to have, does not block readiness


@dataclass
class CoverageResult:
    """Full coverage report."""

    areas: list[AreaStatus] = field(default_factory=list)

    @property
    def _required(self) -> list[AreaStatus]:
        return [a for a in self.areas if not a.recommended]

    @property
    def covered_count(self) -> int:
        return sum(1 for a in self._required if a.covered)

    @property
    def total_count(self) -> int:
        return len(self._required)

    @property
    def coverage_pct(self) -> float:
        if not self._required:
            return 0.0
        return self.covered_count / self.total_count * 100.0

    @property
    def is_complete(self) -> bool:
        """True when all *required* areas are covered."""
        return all(a.covered for a in self._required)

    @property
    def missing(self) -> list[str]:
        return [a.name for a in self._required if not a.covered]

    @property
    def covered(self) -> list[str]:
        return [a.name for a in self._required if a.covered]

    @property
    def missing_recommended(self) -> list[str]:
        """Recommended areas that are not yet covered."""
        return [a.name for a in self.areas if a.recommended and not a.covered]


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _cars_question_count() -> int:
    """Count CARS questions in the question bank (manual + eval-passed AI)."""
    count = 0
    for path in (_QUESTIONS_PATH, _GENERATED_PATH):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for q in data.get("questions", []):
                if q.get("topic") == "CARS":
                    if path == _QUESTIONS_PATH or q.get("eval_passed", False):
                        count += 1
        except Exception:  # noqa: BLE001
            pass
    return count


def _deck_card_count(col: Collection, subdeck_component: str) -> int:
    """Return the number of cards in any deck whose name contains the component."""
    rows = col.db.all(
        """
        select count(*)
        from cards c
        join decks d on d.id = (case when c.odid != 0 then c.odid else c.did end)
        where instr(char(31) || d.name || char(31),
                    char(31) || ? || char(31)) > 0
        """,
        subdeck_component,
    )
    return int(rows[0][0]) if rows else 0


def _check_area(
    col: Collection, area_name: str, subdeck: str | None, recommended: bool
) -> AreaStatus:
    if subdeck is None:
        n = _cars_question_count()
        if n > 0:
            return AreaStatus(area_name, covered=True,
                              note=f"{n} practice questions", recommended=recommended)
        return AreaStatus(area_name, covered=False,
                          note="No CARS questions found in question bank",
                          recommended=recommended)
    n = _deck_card_count(col, subdeck)
    if n > 0:
        return AreaStatus(area_name, covered=True,
                          note=f"{n:,} flashcards", recommended=recommended)
    return AreaStatus(area_name, covered=False,
                      note=f'No "{subdeck}" deck found in collection',
                      recommended=recommended)


def compute(col: Collection) -> CoverageResult:
    """Build the full coverage report for the current collection."""
    result = CoverageResult()
    for area_name, subdeck in REQUIRED_AREAS:
        result.areas.append(_check_area(col, area_name, subdeck, recommended=False))
    for area_name, subdeck in RECOMMENDED_AREAS:
        result.areas.append(_check_area(col, area_name, subdeck, recommended=True))
    return result


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

_CSS = """
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; padding: 16px; }
.coverage-header { margin-bottom: 20px; }
.coverage-pct { font-size: 40px; font-weight: 800; letter-spacing: -0.03em; }
.coverage-pct.complete { color: #4ade80; }
.coverage-pct.incomplete { color: #f87171; }
.coverage-label { font-size: 13px; opacity: 0.45; margin-left: 8px; }
.coverage-bar-wrap { background: rgba(255,255,255,0.08); border-radius: 20px;
                     height: 6px; width: 260px; margin-top: 8px; }
.coverage-bar { height: 6px; border-radius: 20px; background: #4ade80; }
.coverage-bar.incomplete { background: linear-gradient(90deg, #7c6ef5, #9d8fff); }
.area-list { list-style: none; padding: 0; margin: 18px 0 0; }
.area-list li { display: flex; align-items: center; gap: 10px;
                padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }
.area-list li:last-child { border-bottom: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.dot.ok  { background: #4ade80; }
.dot.bad { background: #f87171; }
.dot.rec { background: #fbbf24; }
.area-name { font-weight: 600; font-size: 14px; min-width: 180px; }
.area-note { font-size: 12px; opacity: 0.45; }
.missing-msg { margin-top: 18px; padding: 12px 16px;
               background: rgba(248,113,113,0.08);
               border-left: 4px solid rgba(248,113,113,0.6);
               border-radius: 8px; font-size: 13px; color: #f87171; }
</style>
"""


def render_html(col: Collection) -> str:
    result = compute(col)
    pct = result.coverage_pct
    pct_class = "complete" if result.is_complete else "incomplete"
    bar_width = int(pct * 2.6)  # 260px max

    rows = ""
    for area in result.areas:
        if area.recommended:
            dot_class = "ok" if area.covered else "rec"
            tag = ' <span style="font-size:11px;opacity:0.5">(recommended)</span>'
        else:
            dot_class = "ok" if area.covered else "bad"
            tag = ""
        rows += (
            f'<li>'
            f'<span class="dot {dot_class}"></span>'
            f'<span class="area-name">{area.name}{tag}</span>'
            f'<span class="area-note">{area.note}</span>'
            f'</li>'
        )

    missing_block = ""
    if not result.is_complete:
        missing_names = ", ".join(result.missing)
        missing_block = (
            f'<div class="missing-msg">'
            f"<strong>Missing areas:</strong> {missing_names}<br>"
            "Add the corresponding AnKing-MCAT subdecks to reach 100% coverage."
            "</div>"
        )

    return f"""
{_CSS}
<div class="coverage-header">
  <span class="coverage-pct {pct_class}">{pct:.0f}%</span>
  <span class="coverage-label">MCAT content coverage
  ({result.covered_count} / {result.total_count} areas)</span>
  <div class="coverage-bar-wrap">
    <div class="coverage-bar {pct_class}" style="width:{bar_width}px"></div>
  </div>
</div>
<ul class="area-list">{rows}</ul>
{missing_block}
"""
