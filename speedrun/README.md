# speedrun/ — standalone CLI directory

This directory contains standalone copies of the core Speedrun modules for use
outside the full Qt app (CLI workflows, dev iteration without a full build,
and the Firebase project config).

## Project Documentation

- [Architecture overview](ARCHITECTURE.md) — how the desktop app, mobile app, adaptive loop, databases, AI generator, and sync system fit together
- [Rust change note](RUST_CHANGE.md) — the Points-at-Stake review ordering added to the Rust scheduler
- [Model descriptions](MODELS.md) — memory, performance, and readiness models with formulas and give-up rules
- [Results report](results_report.md) — calibration results and honest interpretation for project submission
- [Re-running tests](#re-running-tests) — exact commands to regenerate the calibration charts and scores

---

## Relationship to `qt/aqt/speedrun/`

The **running Speedrun app** imports from `qt/aqt/speedrun/` (the integrated Qt
package). This directory is a CLI/standalone sibling that mirrors most of the
same modules but may lag the Qt version between syncs.

If you are reading the source to understand how something works, start in
`qt/aqt/speedrun/`.

## Contents

| Path | Purpose |
|------|---------|
| `memory_score.py` | CLI: print a memory-score report from a collection copy |
| `performance_score.py` | CLI: quiz questions interactively, print accuracy report |
| `speedrun_loop.py` | Core adaptive-loop logic (imported by the Qt driver) |
| `question_generator.py` | CLI: generate questions with GPT-4o from OpenStax source |
| `eval.py` | CLI: eval-filter generated questions for quality |
| `auto_generator.py` | Background generation trigger (also used by Qt driver) |
| `openstax_fetcher.py` | CLI: fetch and cache OpenStax HTML/XML source content |
| `question_sync.py` | Firestore sync helpers |
| `questions.json` | Curated hand-written question bank |
| `generated_questions.json` | AI-generated questions (eval-passed) |
| `eval_results.json` | Latest eval run output |
| `firebase/` | Firestore project config and security rules |
| `openstax_cache/` | Cached OpenStax content (gitignored) |

## Model Descriptions

Detailed one-page descriptions of the memory, performance, and readiness models — including formulas, aggregation methods, and give-up rules — are in [`MODELS.md`](MODELS.md).

---

## Re-running Tests

Run these from the **repo root** (`/Users/skyeflowers/anki`). Anki must be closed so the collection database is not locked.

**Memory model calibration** — plots predicted FSRS retrievability vs. actual recall rate on the held-out 20% of review history, and prints the Brier score:

```bash
out/pyenv/bin/python qt/aqt/speedrun/calibrate_memory.py
```

Outputs `calibration_memory.png` in the repo root.

**Performance model calibration** — reports held-out accuracy (last 20% of answered questions) broken down by MCAT section and topic:

```bash
out/pyenv/bin/python qt/aqt/speedrun/calibrate_performance.py
```

Outputs `calibration_performance.png` in the repo root.

Results are saved to [`speedrun/results_report.md`](results_report.md); the memory calibration chart is saved to [`proof/calibration_chart.png`](proof/calibration_chart.png) and the performance chart to [`proof/calibration_performance.png`](proof/calibration_performance.png).

Both scripts auto-detect the Anki collection at the default path (`~/Library/Application Support/Anki2/User 1/collection.anki2` on macOS). To use a different collection, pass its path as the first argument:

```bash
out/pyenv/bin/python qt/aqt/speedrun/calibrate_memory.py /path/to/collection.anki2
out/pyenv/bin/python qt/aqt/speedrun/calibrate_performance.py /path/to/collection.anki2
```

---

## CLI usage examples

```bash
# Print a memory-score report (safe while Anki is open — reads a temp copy)
python -m speedrun.memory_score

# Run the interactive question quiz and print accuracy
python -m speedrun.performance_score --quiz

# Generate new questions for a topic
python -m speedrun.question_generator --topic Biology --count 10

# Eval-filter generated questions
python -m speedrun.eval

# Deploy updated Firestore security rules
cd speedrun/firebase && npx firebase-tools deploy --only firestore:rules
```
