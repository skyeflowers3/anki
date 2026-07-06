# speedrun/ — standalone CLI directory

This directory contains standalone copies of the core Speedrun modules for use
outside the full Qt app (CLI workflows, dev iteration without a full build,
and the Firebase project config).

## Project Documentation

- [Files touched](FILES_TOUCHED.md) — every file modified or created in this fork, grouped by category with one-line descriptions
- [Architecture overview](ARCHITECTURE.md) — how the desktop app, mobile app, adaptive loop, databases, AI generator, and sync system fit together
- [Rust change note](RUST_CHANGE.md) — the Points-at-Stake review ordering added to the Rust scheduler
- [Model descriptions](MODELS.md) — memory, performance, and readiness models with formulas and give-up rules
- [Results report](results_report.md) — calibration results and honest interpretation for project submission
- [Re-running tests](#re-running-tests) — exact commands to regenerate the calibration charts and scores

---

## Bundled Deck & Installer

### What ships with the app

`qt/aqt/speedrun/mcat_deck.apkg` (224 MB) is the full MCAT Study Blocks deck
exported from the development collection. It is bundled inside the Briefcase
installer so that students receive all flashcard content on first launch —
no manual import required.

**First-run import flow** (`qt/aqt/main.py → _maybe_import_bundled_mcat_deck`):

1. On profile load, Anki checks the `speedrun_deck_imported` flag in profile metadata.
2. If not set, it checks whether the deck root (`AnKing-MCAT`) already exists.
3. If absent, it imports `mcat_deck.apkg` silently in a background thread.
4. After import, sets the flag so the check never runs again.

### Building the macOS DMG

Requires [Briefcase](https://briefcase.readthedocs.io) and the bundled deck in
place at `qt/aqt/speedrun/mcat_deck.apkg`.

```bash
# Install Briefcase (one-time)
pip install briefcase

# Build the .app bundle
cd qt/installer/app
briefcase build macOS

# Package into a distributable DMG
briefcase package macOS
```

The DMG is written to `qt/installer/app/dist/Speedrun-1.0.0.dmg`. The app is
named **Speedrun** (set in `qt/installer/app/pyproject.toml`).

> **Note on Gatekeeper:** The DMG is signed with an ad-hoc identity (no Apple
> Developer certificate). On another Mac, Gatekeeper will block it on first
> launch. To open it, right-click the app → **Open** → **Open** in the dialog.
> This is a one-time step. Alternatively, reviewers can run the app directly
> from source with `just run` (no Gatekeeper prompt).

### Updating the bundled deck

Re-export the MCAT Study Blocks deck from Anki (File → Export, include
scheduling, format = Anki Deck Package), then overwrite the bundled file:

```bash
cp "MCAT Study Blocks.apkg" qt/aqt/speedrun/mcat_deck.apkg
```

Then rebuild the DMG using the steps above.

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

### Key Qt/UI files (in `qt/aqt/speedrun/`)

| File | Purpose |
|------|---------|
| `home.py` | MCAT home screen — startup landing page with action buttons and live score summary |
| `driver.py` | Adaptive loop Qt driver + `PracticeQuizController` (20–60 question practice mode) |
| `coverage_map.py` | MCAT content coverage tracker — required vs. recommended areas, Coverage Stats tab |
| `readiness_score.py` | 472–528 MCAT score projection with confidence interval; Readiness Stats tab |
| `mcat_deck.apkg` | Bundled MCAT flashcard deck — auto-imported on first launch |

## Model Descriptions

Detailed one-page descriptions of the memory, performance, and readiness models — including formulas, aggregation methods, and give-up rules — are in [`MODELS.md`](MODELS.md).

---

## Re-running Tests

Run all three commands from the **repo root** (`/Users/skyeflowers/anki`). Anki must be closed for the calibration scripts so the collection database is not locked.

| Script | What it checks | Output |
|--------|---------------|--------|
| `calibrate_memory.py` | FSRS predicted R vs. actual recall on held-out reviews; prints Brier score | `calibration_memory.png` |
| `calibrate_performance.py` | Held-out question accuracy by MCAT section (last 20% by date) | `calibration_performance.png` |
| `leakage_check.py` | Near-duplicate detection between test and training questions (threshold 0.8) | `CLEAN` / `FLAGGED` in terminal |

```bash
# Memory model calibration (Brier score + chart)
out/pyenv/bin/python qt/aqt/speedrun/calibrate_memory.py

# Performance model calibration (section accuracy + chart)
out/pyenv/bin/python qt/aqt/speedrun/calibrate_performance.py

# Leakage check (no collection needed)
out/pyenv/bin/python qt/aqt/speedrun/leakage_check.py
```

Results are saved to [`speedrun/results_report.md`](results_report.md); charts are saved to [`proof/calibration_chart.png`](proof/calibration_chart.png) and [`proof/calibration_performance.png`](proof/calibration_performance.png).

To use a non-default collection path, pass it as the first argument to either calibration script:

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
