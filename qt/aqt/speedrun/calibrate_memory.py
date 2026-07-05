#!/usr/bin/env python3
"""Memory model calibration for the Speedrun FSRS memory score.

Usage (Anki must be closed so the DB is not locked):

    out/pyenv/bin/python qt/aqt/speedrun/calibrate_memory.py

Outputs:
    • calibration_memory.png  — predicted vs actual recall rate by bucket
    • Brier score printed to stdout

How it works
------------
FSRS schedules each card so that retrievability R = 0.9 at the due date.
The formula is:

    R(t) = 0.9 ^ (t / S)

where t = elapsed days since last review and S = the scheduled interval
(which FSRS sets equal to the stability, i.e. the interval that yields R=0.9).

For every type-1 review (scheduled review, not learning step) in the revlog
we can reconstruct the predicted R from the stored lastIvl (the scheduled
interval before the review) and the actual elapsed time between reviews.
The actual outcome is 1 (recalled) if ease >= 2, else 0 (forgotten).

The held-out set is the most recent 20% of review rows by date.
"""

from __future__ import annotations

import math
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate collection
# ---------------------------------------------------------------------------

DEFAULT_PATHS = [
    Path.home() / "Library/Application Support/Anki2/User 1/collection.anki2",
    Path.home() / ".local/share/Anki2/User 1/collection.anki2",
    Path.home() / "AppData/Roaming/Anki2/User 1/collection.anki2",
]


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
# Pull review rows
# ---------------------------------------------------------------------------

def _fetch_reviews(db_path: Path) -> list[tuple[int, int, float, float]]:
    """Return (id_ms, ease, predicted_R, elapsed_days) for type-1 reviews.

    Skips rows where lastIvl <= 0 (learning steps) or elapsed_days <= 0
    (same-day double reviews which corrupt the R estimate).
    We work from a temporary copy so we never touch the live DB.
    """
    tmpdir = tempfile.mkdtemp(prefix="speedrun-calib-")
    tmp = Path(tmpdir) / "collection.anki2"
    shutil.copy2(db_path, tmp)

    rows: list[tuple[int, int, float, float]] = []
    try:
        con = sqlite3.connect(tmp)
        # Fetch type-1 (review) rows with their predecessor timestamp per card.
        # We use a window LAG to get the previous review timestamp for the
        # same card, which lets us compute the actual elapsed days.
        sql = """
        select
            id,
            ease,
            lastIvl,
            lag(id) over (partition by cid order by id) as prev_id
        from revlog
        where type = 1
          and lastIvl > 0
        order by id
        """
        for review_id, ease, last_ivl, prev_id in con.execute(sql):
            if prev_id is None:
                # No previous review for this card — skip (can't compute elapsed).
                continue
            elapsed_days = (review_id - prev_id) / 86_400_000.0
            if elapsed_days <= 0:
                continue
            predicted_r = 0.9 ** (elapsed_days / last_ivl)
            predicted_r = max(0.0, min(1.0, predicted_r))
            rows.append((review_id, ease, predicted_r, elapsed_days))
        con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    return rows


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

NUM_BUCKETS = 10


def _calibrate(rows: list[tuple[int, int, float, float]]) -> None:
    if not rows:
        sys.exit("No type-1 review rows found in the collection.")

    # Hold out the last 20% by review timestamp (id).
    rows.sort(key=lambda r: r[0])
    cutoff = int(len(rows) * 0.8)
    held_out = rows[cutoff:]

    print(f"Total type-1 reviews:   {len(rows):,}")
    print(f"Training set:           {cutoff:,}")
    print(f"Held-out set:           {len(held_out):,}")

    if not held_out:
        sys.exit("Held-out set is empty — need more review history.")

    # Bucket by predicted R.
    bucket_pred: list[list[float]] = [[] for _ in range(NUM_BUCKETS)]
    bucket_actual: list[list[int]] = [[] for _ in range(NUM_BUCKETS)]

    brier_sum = 0.0
    for _, ease, pred_r, _ in held_out:
        actual = 1 if ease >= 2 else 0
        brier_sum += (pred_r - actual) ** 2
        bucket_idx = min(int(pred_r * NUM_BUCKETS), NUM_BUCKETS - 1)
        bucket_pred[bucket_idx].append(pred_r)
        bucket_actual[bucket_idx].append(actual)

    brier = brier_sum / len(held_out)
    print(f"\nBrier score (lower = better, 0 = perfect): {brier:.4f}")

    # Build per-bucket summary.
    xs: list[float] = []  # mean predicted R in bucket
    ys: list[float] = []  # actual recall rate in bucket
    sizes: list[int] = []

    for i in range(NUM_BUCKETS):
        if not bucket_pred[i]:
            continue
        xs.append(sum(bucket_pred[i]) / len(bucket_pred[i]))
        ys.append(sum(bucket_actual[i]) / len(bucket_actual[i]))
        sizes.append(len(bucket_pred[i]))

    if not xs:
        sys.exit("No data in any bucket — cannot plot.")

    _plot(xs, ys, sizes, brier, len(held_out))


# ---------------------------------------------------------------------------
# Plot
# ---------------------------------------------------------------------------

def _plot(
    xs: list[float],
    ys: list[float],
    sizes: list[int],
    brier: float,
    n: int,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print(
            "\nmatplotlib not installed — skipping chart.\n"
            "Install with: pip install matplotlib"
        )
        _print_table(xs, ys, sizes)
        return

    fig, ax = plt.subplots(figsize=(7, 6))

    # Perfect calibration diagonal.
    ax.plot([0, 1], [0, 1], "k--", linewidth=1, label="Perfect calibration")

    # Scatter sized by bucket count.
    max_size = max(sizes)
    point_sizes = [80 + 400 * (s / max_size) for s in sizes]
    scatter = ax.scatter(
        xs, ys,
        s=point_sizes,
        c=xs,
        cmap="RdYlGn",
        vmin=0, vmax=1,
        edgecolors="black",
        linewidths=0.6,
        zorder=3,
        label="Observed recall",
    )

    # Annotate each point with its count.
    for x, y, s in zip(xs, ys, sizes):
        ax.annotate(
            f"n={s:,}",
            (x, y),
            textcoords="offset points",
            xytext=(6, 4),
            fontsize=7,
            color="#444",
        )

    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    ax.set_xlabel("Predicted retrievability (FSRS)", fontsize=12)
    ax.set_ylabel("Actual recall rate", fontsize=12)
    ax.set_title(
        f"Memory Model Calibration (n={n:,} held-out reviews)\n"
        f"Brier score = {brier:.4f}",
        fontsize=13,
    )
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3)

    plt.colorbar(scatter, ax=ax, label="Predicted R")
    plt.tight_layout()

    out = Path("calibration_memory.png")
    plt.savefig(out, dpi=150)
    print(f"\nChart saved to: {out.resolve()}")


def _print_table(xs: list[float], ys: list[float], sizes: list[int]) -> None:
    print(f"\n{'Predicted R':>12}  {'Actual recall':>13}  {'n':>6}")
    print("-" * 38)
    for x, y, s in zip(xs, ys, sizes):
        print(f"{x:>12.3f}  {y:>13.3f}  {s:>6,}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    col_path = _find_collection()
    print(f"Collection: {col_path}\n")
    reviews = _fetch_reviews(col_path)
    _calibrate(reviews)
