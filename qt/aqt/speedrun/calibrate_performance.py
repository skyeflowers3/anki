#!/usr/bin/env python3
"""Performance model calibration for the Speedrun MCAT performance score.

Usage (Anki must be closed so the DB is not locked):

    out/pyenv/bin/python qt/aqt/speedrun/calibrate_performance.py

Outputs:
    • calibration_performance.png  — per-section accuracy bar chart (held-out)
    • Summary table printed to stdout

The held-out set is the most recent 20% of answered questions by answered_at.
Accuracy = answer_correct / total answered (matching the live score formula exactly).
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — must match performance_score.py exactly
# ---------------------------------------------------------------------------

PERFORMANCE_TABLE = "speedrun_performance"

SUBDECK_TO_SECTION: dict[str, str] = {
    "Biology": "B/B",
    "Biochemistry": "B/B",
    "Essential-Equations": "B/B",
    "General-Chemistry": "C/P",
    "Organic-Chemistry": "C/P",
    "Physics-and-Math": "C/P",
    "Behavioral": "P/S",
    "CARS": "CARS",
}

SECTION_ORDER = ["B/B", "C/P", "P/S", "CARS"]

SECTION_NAMES = {
    "B/B": "Biological & Biochemical Foundations",
    "C/P": "Chemical & Physical Foundations",
    "P/S": "Psychological, Social & Biological Foundations",
    "CARS": "Critical Analysis and Reasoning Skills",
}

DEFAULT_PATHS = [
    Path.home() / "Library/Application Support/Anki2/User 1/collection.anki2",
    Path.home() / ".local/share/Anki2/User 1/collection.anki2",
    Path.home() / "AppData/Roaming/Anki2/User 1/collection.anki2",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class TopicResult:
    name: str
    answered: int = 0
    correct: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.answered if self.answered else 0.0


@dataclass
class SectionResult:
    code: str
    name: str
    topics: list[TopicResult] = field(default_factory=list)

    @property
    def answered(self) -> int:
        return sum(t.answered for t in self.topics)

    @property
    def correct(self) -> int:
        return sum(t.correct for t in self.topics)

    @property
    def accuracy(self) -> float:
        return self.correct / self.answered if self.answered else 0.0


# ---------------------------------------------------------------------------
# Collection path
# ---------------------------------------------------------------------------

def _find_collection() -> Path:
    if len(sys.argv) > 1:
        p = Path(sys.argv[1])
        if p.exists():
            return p
        sys.exit(f"Collection not found: {p}")
    for p in DEFAULT_PATHS:
        if p.exists():
            return p
    sys.exit(
        "Could not find collection.anki2. Pass its path as the first argument.\n"
        "Make sure Anki is closed before running this script."
    )


# ---------------------------------------------------------------------------
# Fetch held-out rows
# ---------------------------------------------------------------------------

def _fetch_held_out(db_path: Path) -> list[tuple[str, int]]:
    """Return (topic, answer_correct) for the held-out 20% of rows."""
    tmpdir = tempfile.mkdtemp(prefix="speedrun-perf-calib-")
    tmp = Path(tmpdir) / "collection.anki2"
    shutil.copy2(db_path, tmp)

    rows: list[tuple[str, int]] = []
    try:
        con = sqlite3.connect(tmp)

        # Count total rows.
        total = con.execute(
            f"select count(*) from {PERFORMANCE_TABLE}"
        ).fetchone()[0]

        if total == 0:
            con.close()
            return []

        cutoff = int(total * 0.8)

        # Select the newest 20% ordered by answered_at.
        held = con.execute(
            f"""
            select topic, answer_correct
            from {PERFORMANCE_TABLE}
            order by answered_at asc
            limit -1 offset {cutoff}
            """
        ).fetchall()
        rows = [(str(r[0]), int(r[1])) for r in held]
        con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return rows


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

def _aggregate(rows: list[tuple[str, int]]) -> list[SectionResult]:
    topics: dict[str, TopicResult] = {
        name: TopicResult(name) for name in SUBDECK_TO_SECTION
    }
    for topic, correct in rows:
        if topic in topics:
            topics[topic].answered += 1
            topics[topic].correct += correct

    sections: list[SectionResult] = []
    for code in SECTION_ORDER:
        topic_list = [
            topics[name]
            for name, sec in SUBDECK_TO_SECTION.items()
            if sec == code
        ]
        sections.append(SectionResult(code, SECTION_NAMES[code], topic_list))
    return sections


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _report(sections: list[SectionResult], total_rows: int, held_n: int) -> None:
    total_correct = sum(s.correct for s in sections)
    total_answered = sum(s.answered for s in sections)

    print(f"{'─' * 60}")
    print(f"  MCAT Performance Score — Held-Out Calibration")
    print(f"{'─' * 60}")
    print(f"  Total questions answered (all time): {total_rows:,}")
    print(f"  Training set (first 80%):            {total_rows - held_n:,}")
    print(f"  Held-out set (last 20%):             {held_n:,}")
    print(f"{'─' * 60}")
    print()

    for sec in sections:
        if sec.answered == 0:
            print(f"  {sec.code:<6}  {sec.name}")
            print(f"          No answers in held-out set")
            print()
            continue
        pct = sec.accuracy * 100
        print(f"  {sec.code:<6}  {sec.name}")
        print(f"          {sec.correct} / {sec.answered} correct = {pct:.1f}%")
        for t in sec.topics:
            if t.answered > 0:
                print(
                    f"            · {t.name:<28} "
                    f"{t.correct}/{t.answered} = {t.accuracy * 100:.1f}%"
                )
        print()

    if total_answered > 0:
        overall_pct = total_correct / total_answered * 100
        print(f"{'─' * 60}")
        print(
            f"  Overall (held-out):  "
            f"{total_correct} / {total_answered} correct = {overall_pct:.1f}%"
        )
        print(f"{'─' * 60}")


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def _plot(sections: list[SectionResult], held_n: int) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("\nmatplotlib not available — skipping chart.")
        return

    codes = [s.code for s in sections if s.answered > 0]
    accuracies = [s.accuracy * 100 for s in sections if s.answered > 0]
    counts = [s.answered for s in sections if s.answered > 0]

    if not codes:
        return

    colors = ["#4C9BE8", "#5CB85C", "#F0AD4E", "#D9534F"]
    color_map = dict(zip(SECTION_ORDER, colors))

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar(
        codes,
        accuracies,
        color=[color_map.get(c, "#888") for c in codes],
        edgecolor="white",
        linewidth=1.2,
        width=0.55,
    )

    # Label each bar with accuracy % and n.
    for bar, acc, n in zip(bars, accuracies, counts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 1.0,
            f"{acc:.1f}%\n(n={n:,})",
            ha="center", va="bottom", fontsize=10, fontweight="bold",
        )

    ax.axhline(50, color="red", linestyle="--", linewidth=0.8, label="Chance (50%)")
    ax.axhline(75, color="green", linestyle=":", linewidth=0.8, label="Target (75%)")
    ax.set_ylim(0, 115)
    ax.set_ylabel("Accuracy (%)", fontsize=12)
    ax.set_xlabel("MCAT Section", fontsize=12)
    ax.set_title(
        f"Performance Score Calibration — Held-Out Set (n={held_n:,})",
        fontsize=13,
    )
    ax.legend(fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    plt.tight_layout()
    out = Path("calibration_performance.png")
    plt.savefig(out, dpi=150)
    print(f"Chart saved to: {out.resolve()}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    col_path = _find_collection()
    print(f"Collection: {col_path}\n")

    all_rows_count = 0
    tmpdir2 = tempfile.mkdtemp(prefix="speedrun-perf-count-")
    tmp2 = Path(tmpdir2) / "col.anki2"
    shutil.copy2(col_path, tmp2)
    con2 = sqlite3.connect(tmp2)
    all_rows_count = con2.execute(
        f"select count(*) from {PERFORMANCE_TABLE}"
    ).fetchone()[0]
    con2.close()
    shutil.rmtree(tmpdir2, ignore_errors=True)

    held_out = _fetch_held_out(col_path)
    if not held_out:
        sys.exit("No performance records found in the collection.")

    sections = _aggregate(held_out)
    _report(sections, all_rows_count, len(held_out))
    _plot(sections, len(held_out))
