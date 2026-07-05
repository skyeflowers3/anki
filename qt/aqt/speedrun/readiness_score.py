"""Compute and display the Speedrun MCAT "readiness score".

The readiness score is a projected MCAT total (472–528) derived from the
student's quiz accuracy across all four MCAT sections:

    B/B  — Biological & Biochemical Foundations
    C/P  — Chemical & Physical Foundations
    P/S  — Psychological, Social & Biological Foundations
    CARS — Critical Analysis and Reasoning Skills

Give-up rule
------------
The readiness score is not shown until every section has met its own
performance-score threshold (see `performance_score.SectionPerformance.has_score`):

    • B/B, C/P: ≥30 total + ≥10 per topic
    • P/S, CARS: ≥30 total

Until then the display lists which sections still need more practice.

Calculation
-----------
Each MCAT section is scored 118–132 (range 14 points).  Random guessing on a
4-choice question corresponds to 25% accuracy and maps to the floor (118).  A
conservative calibration factor (CALIBRATION = 0.92) is applied to account for
the fact that AI-generated practice questions tend to be slightly easier than
the real exam.

    eff   = accuracy * CALIBRATION
    score = 118 + clamp((eff - 0.25) / 0.75, 0, 1) * 14

Total score = sum of the four section scores.

Range / uncertainty
-------------------
Per-section standard error is derived from the binomial sampling variance of
accuracy, then propagated through the linear score formula.  Assuming the four
sections are independent, the errors add in quadrature:

    se_section  = sqrt(p*(1-p)/n) / 0.75 * 14 * CALIBRATION
    total_se    = sqrt(sum(se_section_i ** 2))
    low, high   = round(total ± 2 * total_se)     # ≈ 95% CI

Confidence label
----------------
Based on total questions answered across all sections:

    < 100   → "low"
    100–300 → "medium"
    > 300   → "high"

Coverage
--------
Fraction of the 8 topics that have ≥ MIN_ANSWERED (5) answered questions.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from anki.collection import Collection

# Slight downward calibration for AI-generated questions relative to real MCAT.
CALIBRATION = 0.92

# Total topics tracked (8: Biology, Biochemistry, Essential-Equations,
# General-Chemistry, Organic-Chemistry, Physics-and-Math, Behavioral, CARS).
TOTAL_TOPICS = 8

# Section codes shown in the readiness breakdown (same order as the exam).
_SECTION_ORDER = ["B/B", "C/P", "P/S", "CARS"]

_SECTION_NAMES = {
    "B/B": "Biological & Biochemical Foundations",
    "C/P": "Chemical & Physical Foundations",
    "P/S": "Psychological, Social & Biological Foundations",
    "CARS": "Critical Analysis and Reasoning Skills",
}


@dataclass
class SectionReadiness:
    """Per-section data used in the readiness calculation."""

    code: str
    name: str
    accuracy: float  # 0–1
    answered: int
    has_score: bool  # whether the section met its minimum threshold
    pending_desc: str = ""  # human-readable "N more needed in topic X" when blocked


@dataclass
class ReadinessResult:
    """Computed readiness score with all display fields."""

    projected: int
    low: int
    high: int
    confidence: str  # "low" | "medium" | "high"
    topics_with_data: int
    total_answered: int
    sections: list[SectionReadiness] = field(default_factory=list)
    last_updated: float = 0.0  # epoch seconds of the most recent answer
    blocked_sections: list[str] = field(default_factory=list)  # empty when all unlocked


def _section_score(accuracy: float) -> float:
    """Map a section accuracy (0–1) to an MCAT section score (118–132)."""
    eff = accuracy * CALIBRATION
    ratio = max(0.0, min(1.0, (eff - 0.25) / 0.75))
    return 118.0 + ratio * 14.0


def _section_se(accuracy: float, n: int) -> float:
    """Propagated standard error of the section score estimate."""
    if n <= 0:
        return 7.0  # maximum uncertainty (half the section range)
    p = max(0.01, min(0.99, accuracy))
    se_accuracy = math.sqrt(p * (1.0 - p) / n)
    return se_accuracy / 0.75 * 14.0 * CALIBRATION


def compute(col: Collection) -> ReadinessResult | None:
    """Return a ReadinessResult, or None if the give-up rule blocks display.

    Imports performance_score lazily so this module is importable even if
    performance_score has not been initialised yet.
    """
    try:
        from aqt.speedrun import performance_score
    except ImportError:
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[3]
        for _p in (repo_root / "pylib", repo_root / "out" / "pylib"):
            if _p.is_dir() and str(_p) not in sys.path:
                sys.path.insert(0, str(_p))
        from aqt.speedrun import performance_score  # type: ignore[no-redef]

    sections_perf = performance_score.compute_sections(col)

    # Build a lookup from section code → SectionPerformance.
    perf_by_code = {s.code: s for s in sections_perf}

    # Also include CARS, which is not in SECTION_ORDER of memory_score but is
    # tracked by performance_score.
    blocked: list[str] = []
    section_readiness: list[SectionReadiness] = []

    for code in _SECTION_ORDER:
        sp = perf_by_code.get(code)
        if sp is None:
            # Section not in results at all — treat as completely blocked.
            blocked.append(code)
            section_readiness.append(
                SectionReadiness(
                    code=code,
                    name=_SECTION_NAMES[code],
                    accuracy=0.0,
                    answered=0,
                    has_score=False,
                    pending_desc="No answers yet",
                )
            )
            continue

        pending_desc = ""
        if not sp.has_score:
            blocked.append(code)
            parts = [f"{name} (needs {n} more)" for name, n in sp.pending_topics]
            if parts:
                pending_desc = "Topics needing more practice: " + ", ".join(parts)
            else:
                needed = max(0, performance_score.SECTION_MIN_ANSWERED - sp.answered)
                pending_desc = f"Answer {needed} more question{'s' if needed != 1 else ''}"

        accuracy = sp.accuracy if sp.answered > 0 else 0.0
        section_readiness.append(
            SectionReadiness(
                code=code,
                name=sp.name,
                accuracy=accuracy,
                answered=sp.answered,
                has_score=sp.has_score,
                pending_desc=pending_desc,
            )
        )

    # If any section is still blocked, return None — do not show a score.
    if blocked:
        # Still build a partial result so the blocked-display can list sections.
        return ReadinessResult(
            projected=0,
            low=0,
            high=0,
            confidence="",
            topics_with_data=0,
            total_answered=sum(s.answered for s in section_readiness),
            sections=section_readiness,
            blocked_sections=blocked,
        )

    # All sections are unlocked — compute the score.
    section_scores = [_section_score(s.accuracy) for s in section_readiness]
    section_ses = [_section_se(s.accuracy, s.answered) for s in section_readiness]

    total = sum(section_scores)
    total_se = math.sqrt(sum(se**2 for se in section_ses))

    projected = round(total)
    low = round(total - 2.0 * total_se)
    high = round(total + 2.0 * total_se)

    total_answered = sum(s.answered for s in section_readiness)

    if total_answered < 100:
        confidence = "low"
    elif total_answered < 300:
        confidence = "medium"
    else:
        confidence = "high"

    # Coverage: topics with ≥ MIN_ANSWERED answers.
    try:
        topic_results = performance_score.fetch_topic_results(col)
        topics_with_data = sum(
            1
            for tr in topic_results.values()
            if tr.answered >= performance_score.MIN_ANSWERED
        )
    except Exception:  # noqa: BLE001
        topics_with_data = sum(1 for s in section_readiness if s.answered > 0)

    # Last updated = timestamp of most recent answer across all topics.
    try:
        row = col.db.first(
            f"select max(answered_at) from {performance_score.PERFORMANCE_TABLE}"
        )
        last_updated = float(row[0]) if row and row[0] is not None else 0.0
    except Exception:  # noqa: BLE001
        last_updated = 0.0

    return ReadinessResult(
        projected=projected,
        low=low,
        high=high,
        confidence=confidence,
        topics_with_data=topics_with_data,
        total_answered=total_answered,
        sections=section_readiness,
        last_updated=last_updated,
        blocked_sections=[],
    )


def _esc(text: str) -> str:
    from html import escape

    return escape(str(text))


def _fmt_age(epoch: float) -> str:
    """Human-readable age string for the last_updated timestamp."""
    if epoch == 0.0:
        return "unknown"
    delta = time.time() - epoch
    if delta < 60:
        return "just now"
    if delta < 3600:
        mins = int(delta / 60)
        return f"{mins} minute{'s' if mins != 1 else ''} ago"
    if delta < 86400:
        hours = int(delta / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(delta / 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"


_READINESS_CSS = """
<style>
.mcat-readiness { max-width: 720px; margin: 0 auto; padding: 20px 16px 48px; }
.mcat-readiness h1 { font-size: 22px; font-weight: 800; margin: 0 0 4px; letter-spacing: -0.02em; }
.mcat-readiness .give-up-rule { opacity: 0.55; font-size: 13px; margin: 0 0 24px; line-height: 1.6; }

.readiness-card {
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    padding: 20px 22px;
    margin-bottom: 20px;
    background: rgba(255,255,255,0.025);
}
.readiness-main-score {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 6px;
}
.readiness-main-score .label {
    opacity: 0.45; font-size: 11px; text-transform: uppercase;
    letter-spacing: 0.06em; font-weight: 700;
}
.readiness-main-score .score { font-size: 44px; font-weight: 800; letter-spacing: -0.03em; }
.readiness-range { opacity: 0.65; font-size: 14px; margin-bottom: 4px; }
.readiness-meta { opacity: 0.5; font-size: 12px; margin-bottom: 16px; }
.readiness-meta span { margin-right: 16px; }

.readiness-conf-low    { color: #f87171; }
.readiness-conf-medium { color: #fbbf24; }
.readiness-conf-high   { color: #4ade80; }

.readiness-section-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 6px; }
.readiness-section-table th,
.readiness-section-table td {
    text-align: left; padding: 7px 10px;
    border-top: 1px solid rgba(255,255,255,0.07);
}
.readiness-section-table th { opacity: 0.45; font-weight: 700; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
.readiness-section-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.readiness-section-table td.muted { opacity: 0.4; }
.readiness-note { opacity: 0.45; font-size: 12px; margin-top: 16px; font-style: italic; line-height: 1.6; }

.readiness-blocked {
    border: 1px solid rgba(255,255,255,0.07);
    border-radius: 14px;
    padding: 16px 18px;
    margin-bottom: 16px;
    background: rgba(255,255,255,0.025);
}
.readiness-blocked h2 { font-size: 15px; font-weight: 700; margin: 0 0 12px; }
.readiness-blocked-list { list-style: none; padding: 0; margin: 0; font-size: 13px; }
.readiness-blocked-list li { padding: 5px 0; }
.readiness-blocked-list li::before { margin-right: 8px; }
.readiness-blocked-list li.done::before { content: "✓"; color: #4ade80; font-weight: 700; }
.readiness-blocked-list li.todo::before { content: "✗"; color: #f87171; font-weight: 700; }
.readiness-blocked-pending { opacity: 0.5; font-size: 12px; margin-left: 4px; }
.readiness-recommendation {
  margin: 16px 0 0;
  padding: 12px 16px;
  background: rgba(251,191,36,0.08);
  border-left: 3px solid #fbbf24;
  border-radius: 8px;
  font-size: 13px;
  color: inherit;
}
</style>
"""


def render_html(col: Collection) -> str:
    """Render the readiness-score report as an HTML body for an AnkiWebView."""
    # Gate on 100% content coverage before computing the score.
    try:
        from aqt.speedrun import coverage_map
    except ImportError:
        coverage_map = None  # type: ignore[assignment]

    _coverage_result = None
    if coverage_map is not None:
        _coverage_result = coverage_map.compute(col)
        if not _coverage_result.is_complete:
            missing = ", ".join(_coverage_result.missing)
            return (
                f"{_READINESS_CSS}"
                '<div class="mcat-readiness">'
                "<h1>MCAT Readiness Score</h1>"
                "<div class='readiness-blocked'>"
                "<p style='font-size:14px;margin:0 0 8px'>"
                "Readiness score unavailable until all MCAT content areas are covered."
                "</p>"
                f"<p style='opacity:0.7;font-size:13px;margin:0'>"
                f"Missing areas: <strong>{missing}</strong>. "
                "Go to the <strong>MCAT Coverage</strong> tab for details."
                "</p>"
                "</div>"
                "</div>"
            )

    result = compute(col)

    parts: list[str] = [_READINESS_CSS, '<div class="mcat-readiness">']
    parts.append("<h1>MCAT Readiness Score</h1>")
    parts.append(
        '<p class="give-up-rule">'
        "A projected total MCAT score (472\u2013528) based on your quiz accuracy "
        "across all four sections. "
        "Requires all four sections to meet their minimum practice thresholds "
        "(30 total questions answered; sections with 3 topics also need "
        "10 per topic). "
        "Until then, this page shows which sections still need more practice."
        "</p>"
    )

    if result is None:
        parts.append(
            "<div class='readiness-blocked'>"
            "<p style='opacity:0.7'>Could not compute readiness score.</p>"
            "</div>"
        )
        parts.append("</div>")
        return "\n".join(parts)

    if result.blocked_sections:
        # Show blocked state with per-section status list.
        parts.append('<div class="readiness-blocked">')
        parts.append(
            "<h2>No readiness score yet \u2014 complete each section first</h2>"
        )
        parts.append('<ul class="readiness-blocked-list">')
        for s in result.sections:
            done_cls = "done" if s.has_score else "todo"
            answered_label = (
                f"{s.answered} answered" if s.answered else "no answers yet"
            )
            li = (
                f'<li class="{done_cls}">'
                f"<strong>{_esc(s.code)}</strong> \u2014 {_esc(s.name)}"
                f' <span class="readiness-blocked-pending">({_esc(answered_label)})'
            )
            if not s.has_score and s.pending_desc:
                li += f" &mdash; {_esc(s.pending_desc)}"
            li += "</span></li>"
            parts.append(li)
        parts.append("</ul>")
        parts.append("</div>")
        parts.append("</div>")
        return "\n".join(parts)

    # Full score display.
    conf_cls = f"readiness-conf-{result.confidence}"
    parts.append('<div class="readiness-card">')

    parts.append('<div class="readiness-main-score">')
    parts.append('<span class="label">Projected MCAT</span>')
    parts.append(f'<span class="score">{result.projected}</span>')
    parts.append("</div>")

    parts.append(
        f'<div class="readiness-range">Likely range: '
        f"<strong>{result.low} \u2013 {result.high}</strong></div>"
    )

    coverage_pct = round(result.topics_with_data / TOTAL_TOPICS * 100)
    parts.append(
        f'<div class="readiness-meta">'
        f'<span class="{conf_cls}">Confidence: {_esc(result.confidence)}</span>'
        f"<span>Coverage: {result.topics_with_data}/{TOTAL_TOPICS} topics "
        f"({coverage_pct}% of exam material practiced)</span>"
        f"<span>Last updated: {_esc(_fmt_age(result.last_updated))}</span>"
        f"</div>"
    )

    # Section breakdown table.
    parts.append('<table class="readiness-section-table">')
    parts.append(
        "<tr>"
        "<th>Section</th>"
        '<th class="num">Accuracy</th>'
        '<th class="num">Answered</th>'
        '<th class="num">Projected score</th>'
        "</tr>"
    )
    for s in result.sections:
        section_proj = round(_section_score(s.accuracy))
        accuracy_str = f"{s.accuracy * 100:.1f}%"
        parts.append(
            f"<tr>"
            f"<td><strong>{_esc(s.code)}</strong> \u2014 {_esc(s.name)}</td>"
            f'<td class="num">{_esc(accuracy_str)}</td>'
            f'<td class="num">{s.answered}</td>'
            f'<td class="num">{section_proj}</td>'
            f"</tr>"
        )
    parts.append("</table>")

    parts.append(
        '<p class="readiness-note">'
        "A 0.92 calibration factor is applied because AI-generated practice "
        "questions tend to be slightly easier than the real exam. "
        "The likely range widens with fewer questions answered and narrows as "
        "you practice more."
        "</p>"
    )

    # Recommendation banner if Essential Equations deck is missing.
    if _coverage_result is not None and _coverage_result.missing_recommended:
        missing_rec = ", ".join(_coverage_result.missing_recommended)
        parts.append(
            f'<div class="readiness-recommendation">'
            f"<strong>Recommendation:</strong> Complete the "
            f"<strong>{missing_rec}</strong> deck to strengthen recall."
            f"</div>"
        )

    parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts)
