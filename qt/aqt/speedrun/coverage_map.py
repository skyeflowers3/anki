"""MCAT content-area coverage map for Speedrun.

Checks whether all seven required MCAT content areas are present in the
current collection and question bank, then reports a coverage percentage and
the list of covered/missing areas.

Coverage rules
--------------
Flashcard-backed areas (6):
    A topic is covered when the corresponding AnKing-MCAT subdeck exists in
    the collection AND contains at least one card.  Subdecks are matched by
    the path component (e.g. "Biology" anywhere in the deck name).

CARS (1):
    CARS has no flashcard deck.  It is covered when at least one CARS question
    exists in the question bank (questions.json or generated_questions.json
    with eval_passed=True).

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
REQUIRED_AREAS: list[tuple[str, str | None]] = [
    ("Behavioral Sciences", "Behavioral"),
    ("Biology", "Biology"),
    ("Biochemistry", "Biochemistry"),
    ("General Chemistry", "General-Chemistry"),
    ("Organic Chemistry", "Organic-Chemistry"),
    ("Physics and Math", "Physics-and-Math"),
    ("CARS", None),  # covered via question bank, not flashcard deck
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
    note: str = ""  # short explanation of how it is covered (or why not)


@dataclass
class CoverageResult:
    """Full coverage report."""

    areas: list[AreaStatus] = field(default_factory=list)

    @property
    def covered_count(self) -> int:
        return sum(1 for a in self.areas if a.covered)

    @property
    def total_count(self) -> int:
        return len(self.areas)

    @property
    def coverage_pct(self) -> float:
        if not self.areas:
            return 0.0
        return self.covered_count / self.total_count * 100.0

    @property
    def is_complete(self) -> bool:
        return self.covered_count == self.total_count

    @property
    def missing(self) -> list[str]:
        return [a.name for a in self.areas if not a.covered]

    @property
    def covered(self) -> list[str]:
        return [a.name for a in self.areas if a.covered]


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


def compute(col: Collection) -> CoverageResult:
    """Build the full coverage report for the current collection."""
    result = CoverageResult()
    for area_name, subdeck in REQUIRED_AREAS:
        if subdeck is None:
            # CARS — covered by question bank
            n = _cars_question_count()
            if n > 0:
                result.areas.append(
                    AreaStatus(area_name, covered=True, note=f"{n} practice questions")
                )
            else:
                result.areas.append(
                    AreaStatus(
                        area_name,
                        covered=False,
                        note="No CARS questions found in question bank",
                    )
                )
        else:
            n = _deck_card_count(col, subdeck)
            if n > 0:
                result.areas.append(
                    AreaStatus(area_name, covered=True, note=f"{n:,} flashcards")
                )
            else:
                result.areas.append(
                    AreaStatus(
                        area_name,
                        covered=False,
                        note=f'No "{subdeck}" deck found in collection',
                    )
                )
    return result


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

_CSS = """
<style>
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       margin: 0; padding: 16px; }
.coverage-header { margin-bottom: 18px; }
.coverage-pct { font-size: 36px; font-weight: 700; }
.coverage-pct.complete { color: #2e7d32; }
.coverage-pct.incomplete { color: #c62828; }
.coverage-label { font-size: 13px; opacity: 0.6; margin-left: 8px; }
.coverage-bar-wrap { background: #e0e0e0; border-radius: 6px;
                     height: 10px; width: 260px; margin-top: 6px; }
.coverage-bar { height: 10px; border-radius: 6px; background: #2e7d32; }
.coverage-bar.incomplete { background: #c62828; }
.area-list { list-style: none; padding: 0; margin: 16px 0 0; }
.area-list li { display: flex; align-items: center; gap: 10px;
                padding: 7px 0; border-bottom: 1px solid rgba(0,0,0,0.07); }
.area-list li:last-child { border-bottom: none; }
.dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.dot.ok  { background: #2e7d32; }
.dot.bad { background: #c62828; }
.area-name { font-weight: 600; font-size: 14px; min-width: 180px; }
.area-note { font-size: 12px; opacity: 0.55; }
.missing-msg { margin-top: 18px; padding: 12px 16px;
               background: #fff3e0; border-left: 4px solid #e65100;
               border-radius: 4px; font-size: 13px; color: #bf360c; }
</style>
"""


def render_html(col: Collection) -> str:
    result = compute(col)
    pct = result.coverage_pct
    pct_class = "complete" if result.is_complete else "incomplete"
    bar_width = int(pct * 2.6)  # 260px max

    rows = ""
    for area in result.areas:
        dot_class = "ok" if area.covered else "bad"
        rows += (
            f'<li>'
            f'<span class="dot {dot_class}"></span>'
            f'<span class="area-name">{area.name}</span>'
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
