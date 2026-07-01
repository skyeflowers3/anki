#!/usr/bin/env python3
"""Compute the Speedrun MCAT "memory score" from an Anki collection.

The memory score is FSRS retrievability (R), aggregated by MCAT section. R is
read straight out of the collection using Anki's own `extract_fsrs_retrievability`
SQL function, so the numbers match exactly what the scheduler would use.

Sections map to AnKing-MCAT subdecks as follows:

    B/B (Biological & Biochemical Foundations)  = Biology + Biochemistry + Essential-Equations
    C/P (Chemical & Physical Foundations)       = General-Chemistry + Organic-Chemistry + Physics-and-Math
    P/S (Psychological, Social & Biological)     = Behavioral

Give-up rule: a section with fewer than MIN_REVIEWED cards that have an FSRS
retrievability value is shown as "not enough data" rather than a misleading
number. MIN_REVIEWED is a documented tunable constant (15 for the demo deck;
raise to ~200 for a full production deck).

This module is used two ways:

* As a CLI (`out/pyenv/bin/python speedrun/memory_score.py`), which opens a
  temporary *copy* of the collection so it never locks or mutates live data.
* As a library imported by the Anki desktop UI (see qt/aqt/stats.py), which
  passes an already-open `Collection` to `compute_sections()` and renders the
  result with `render_html()`.

`extract_fsrs_retrievability` is registered by Anki's Rust backend, so any
caller must be running against a real Anki backend (the built pyenv, or the
running desktop app).
"""

from __future__ import annotations

import argparse
import statistics
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

# Make the in-tree `anki` package importable without a wheel install when run
# as a standalone CLI. The generated protobuf/native files live under out/pylib,
# the hand-written source under pylib; together they form the `anki` namespace
# package. When imported from inside the running app, `anki` is already
# importable and these become harmless no-ops.
_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (_REPO_ROOT / "pylib", _REPO_ROOT / "out" / "pylib"):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Below the threshold, no score is shown for a section. Tunable: 15 suits the
# 20-card-per-subdeck demo deck; a full production deck should use ~200.
MIN_REVIEWED = 15

# Internal Anki deck-name separator (decks table stores full paths joined by it).
DECK_SEP = "\x1f"

# Subdeck (path component) -> MCAT section code. Insertion order also controls
# the order subdecks are listed within each section in the report.
SUBDECK_TO_SECTION: dict[str, str] = {
    "Biology": "B/B",
    "Biochemistry": "B/B",
    "Essential-Equations": "B/B",
    "General-Chemistry": "C/P",
    "Organic-Chemistry": "C/P",
    "Physics-and-Math": "C/P",
    "Behavioral": "P/S",
}

# Display order + human-readable section names.
SECTION_ORDER = ["B/B", "C/P", "P/S"]
SECTION_NAMES = {
    "B/B": "Biological & Biochemical Foundations",
    "C/P": "Chemical & Physical Foundations",
    "P/S": "Psychological, Social & Biological Foundations",
}

DEFAULT_COLLECTION_PATHS = [
    Path.home() / "Library/Application Support/Anki2/User 1/collection.anki2",
    Path.home() / ".local/share/Anki2/User 1/collection.anki2",
]


@dataclass
class Bucket:
    """Accumulates retrievability values for a subdeck."""

    name: str
    retrievabilities: list[float] = field(default_factory=list)

    @property
    def reviewed(self) -> int:
        return len(self.retrievabilities)

    @property
    def average(self) -> float:
        return statistics.fmean(self.retrievabilities)

    @property
    def minimum(self) -> float:
        return min(self.retrievabilities)

    @property
    def maximum(self) -> float:
        return max(self.retrievabilities)


@dataclass
class SectionScore:
    """Aggregated memory score for one MCAT section."""

    code: str
    name: str
    subdecks: list[Bucket]

    @property
    def values(self) -> list[float]:
        return [r for b in self.subdecks for r in b.retrievabilities]

    @property
    def reviewed(self) -> int:
        return len(self.values)

    @property
    def has_score(self) -> bool:
        return self.reviewed >= MIN_REVIEWED

    @property
    def needed(self) -> int:
        return max(0, MIN_REVIEWED - self.reviewed)

    @property
    def average(self) -> float:
        return statistics.fmean(self.values)

    @property
    def minimum(self) -> float:
        return min(self.values)

    @property
    def maximum(self) -> float:
        return max(self.values)


def resolve_collection_path(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            sys.exit(f"error: collection not found at {p}")
        return p
    for candidate in DEFAULT_COLLECTION_PATHS:
        if candidate.exists():
            return candidate
    searched = "\n  ".join(str(p) for p in DEFAULT_COLLECTION_PATHS)
    sys.exit(
        "error: could not find a collection at any default location:\n  "
        + searched
        + "\nPass --collection /path/to/collection.anki2"
    )


def subdeck_for_deck_name(name: str) -> str | None:
    """Return the mapped subdeck for a full deck path, or None if unmapped."""
    for component in name.split(DECK_SEP):
        if component in SUBDECK_TO_SECTION:
            return component
    return None


def fetch_buckets(col: Collection) -> dict[str, Bucket]:
    """Bucket FSRS retrievability by subdeck for an already-open collection.

    Cards without an FSRS retrievability value (never reviewed / no memory
    state) are skipped, since they cannot contribute to a memory score.
    """
    timing = col.sched._timing_today()
    now = int(time.time())
    rows = col.db.all(
        """
        select d.name,
               extract_fsrs_retrievability(
                   c.data,
                   case when c.odue != 0 then c.odue else c.due end,
                   c.ivl, ?, ?, ?)
        from cards c
        join decks d
          on d.id = (case when c.odid != 0 then c.odid else c.did end)
        """,
        timing.days_elapsed,
        timing.next_day_at,
        now,
    )

    buckets: dict[str, Bucket] = {name: Bucket(name) for name in SUBDECK_TO_SECTION}
    for deck_name, retrievability in rows:
        if retrievability is None:
            continue
        subdeck = subdeck_for_deck_name(deck_name)
        if subdeck is None:
            continue
        buckets[subdeck].retrievabilities.append(float(retrievability))
    return buckets


def build_sections(buckets: dict[str, Bucket]) -> list[SectionScore]:
    sections: list[SectionScore] = []
    for code in SECTION_ORDER:
        subdecks = [
            buckets[name]
            for name, section in SUBDECK_TO_SECTION.items()
            if section == code
        ]
        sections.append(SectionScore(code, SECTION_NAMES[code], subdecks))
    return sections


def compute_sections(col: Collection) -> list[SectionScore]:
    """Convenience entry point for callers holding an open collection."""
    return build_sections(fetch_buckets(col))


def fetch_retrievabilities(collection_path: Path) -> dict[str, Bucket]:
    """Open a temporary copy of the collection and bucket R values by subdeck.

    Used by the CLI so it never locks or mutates the live collection (safe to
    run while Anki is open).
    """
    from anki.collection import Collection

    tmpdir = tempfile.mkdtemp(prefix="speedrun-memscore-")
    tmp_col = Path(tmpdir) / "collection.anki2"
    try:
        shutil.copy2(collection_path, tmp_col)
        col = Collection(str(tmp_col))
        try:
            return fetch_buckets(col)
        finally:
            col.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def render_text(sections: list[SectionScore]) -> str:
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("MCAT Memory Score  (FSRS retrievability by section)")
    lines.append(
        f"Give-up rule: no score below {MIN_REVIEWED} reviewed cards per section"
    )
    lines.append("=" * 66)

    for section in sections:
        lines.append("")
        lines.append(f"{section.code} - {section.name}")
        lines.append("-" * 66)

        if section.has_score:
            lines.append(
                f"  Memory score: {pct(section.average)}   "
                f"range {pct(section.minimum)} - {pct(section.maximum)}   "
                f"({section.reviewed} reviewed cards)"
            )
        else:
            lines.append(
                f"  Not enough data: {section.reviewed} reviewed card(s). "
                f"Review {section.needed} more to unlock this section's score."
            )

        lines.append("  Subdeck breakdown:")
        for b in section.subdecks:
            if b.reviewed == 0:
                lines.append(f"    - {b.name:<20} no reviewed cards")
            else:
                lines.append(
                    f"    - {b.name:<20} {pct(b.average)}   "
                    f"range {pct(b.minimum)} - {pct(b.maximum)}   "
                    f"({b.reviewed} reviewed)"
                )

    lines.append("")
    lines.append("=" * 66)
    return "\n".join(lines)


_MEMORY_CSS = """
<style>
.mcat-memory { max-width: 720px; margin: 0 auto; padding: 20px 16px 40px; }
.mcat-memory h1 { font-size: 20px; margin: 0 0 4px; }
.mcat-memory .give-up-rule { opacity: 0.7; font-size: 13px; margin: 0 0 20px; }
.mcat-section {
    border: 1px solid rgba(128, 128, 128, 0.3);
    border-radius: 8px;
    padding: 14px 16px;
    margin-bottom: 16px;
}
.mcat-section .section-head { display: flex; align-items: baseline; gap: 8px; }
.mcat-section .section-code { font-weight: 700; font-size: 15px; }
.mcat-section .section-name { opacity: 0.7; font-size: 13px; }
.mcat-score { margin: 10px 0 6px; }
.mcat-score .value { font-size: 26px; font-weight: 700; }
.mcat-score .range { opacity: 0.7; font-size: 13px; margin-left: 8px; }
.mcat-score .count { opacity: 0.55; font-size: 12px; margin-left: 6px; }
.mcat-nodata { margin: 10px 0 6px; opacity: 0.75; font-style: italic; }
table.mcat-subdecks { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 13px; }
table.mcat-subdecks th, table.mcat-subdecks td {
    text-align: left; padding: 5px 8px;
    border-top: 1px solid rgba(128, 128, 128, 0.2);
}
table.mcat-subdecks th { opacity: 0.6; font-weight: 600; }
table.mcat-subdecks td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.mcat-subdecks td.muted { opacity: 0.5; }
</style>
"""


def _esc(text: str) -> str:
    from html import escape

    return escape(text)


def render_html(sections: list[SectionScore]) -> str:
    """Render the memory-score report as an HTML body for an AnkiWebView."""
    parts: list[str] = [_MEMORY_CSS, '<div class="mcat-memory">']
    parts.append("<h1>MCAT Memory Score</h1>")
    parts.append(
        '<p class="give-up-rule">FSRS retrievability by section. '
        f"No score is shown for a section with fewer than {MIN_REVIEWED} "
        "reviewed cards.</p>"
    )

    for section in sections:
        parts.append('<div class="mcat-section">')
        parts.append(
            '<div class="section-head">'
            f'<span class="section-code">{_esc(section.code)}</span>'
            f'<span class="section-name">{_esc(section.name)}</span>'
            "</div>"
        )

        if section.has_score:
            parts.append(
                '<div class="mcat-score">'
                f'<span class="value">{pct(section.average)}</span>'
                f'<span class="range">range {pct(section.minimum)} - '
                f"{pct(section.maximum)}</span>"
                f'<span class="count">({section.reviewed} reviewed cards)</span>'
                "</div>"
            )
        else:
            parts.append(
                '<div class="mcat-nodata">'
                f"Not enough data: {section.reviewed} reviewed card(s). "
                f"Review {section.needed} more to unlock this section&rsquo;s score."
                "</div>"
            )

        parts.append('<table class="mcat-subdecks">')
        parts.append(
            "<tr><th>Subdeck</th><th class=\"num\">Memory</th>"
            '<th class="num">Range</th><th class="num">Reviewed</th></tr>'
        )
        for b in section.subdecks:
            if b.reviewed == 0:
                parts.append(
                    f"<tr><td>{_esc(b.name)}</td>"
                    '<td class="num muted" colspan="2">no reviewed cards</td>'
                    '<td class="num muted">0</td></tr>'
                )
            else:
                parts.append(
                    f"<tr><td>{_esc(b.name)}</td>"
                    f'<td class="num">{pct(b.average)}</td>'
                    f'<td class="num">{pct(b.minimum)} - {pct(b.maximum)}</td>'
                    f'<td class="num">{b.reviewed}</td></tr>'
                )
        parts.append("</table>")
        parts.append("</div>")

    parts.append("</div>")
    return "\n".join(parts)


def print_report(sections: list[SectionScore]) -> None:
    print(render_text(sections))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-c",
        "--collection",
        help="Path to collection.anki2 (defaults to the standard Anki location)",
    )
    args = parser.parse_args()

    collection_path = resolve_collection_path(args.collection)
    print(f"Reading collection: {collection_path}")
    buckets = fetch_retrievabilities(collection_path)
    print_report(build_sections(buckets))


if __name__ == "__main__":
    main()
