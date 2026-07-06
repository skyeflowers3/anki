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

Requires the bundled deck at `qt/aqt/speedrun/mcat_deck.apkg` (export from
Anki: File → Export → MCAT Study Blocks → Anki Deck Package, include
scheduling, save to that path).

```bash
cd /path/to/anki

# 1. Rebuild aqt/anki wheels with latest source changes
just wheels

# 2. Build the app bundle (uses the proper Anki build script)
out/pyenv/bin/python qt/tools/build_installer.py --version 26.05 build \
  --aqt_wheel out/wheels/aqt-26.5-py3-none-any.whl \
  --anki_wheel out/wheels/anki-26.5-cp310-abi3-macosx_12_0_x86_64.whl

# 3. Fix the stub binary (Briefcase downloads a Python 3.12 stub but the
#    support package is Python 3.13 — replace it with the correct one)
curl -L "https://briefcase-support.s3.amazonaws.com/python/3.13/macOS/GUI-Stub-3.13-b13.zip" \
  -o /tmp/stub-3.13.zip
unzip -o /tmp/stub-3.13.zip -d /tmp/stub-3.13
cp /tmp/stub-3.13/Stub \
  "out/installer/build/anki/macos/app/Speedrun.app/Contents/MacOS/Speedrun"
codesign --force --sign - \
  "out/installer/build/anki/macos/app/Speedrun.app/Contents/MacOS/Speedrun"

# 4. Copy the bundled MCAT deck into the app bundle
cp qt/aqt/speedrun/mcat_deck.apkg \
  "out/installer/build/anki/macos/app/Speedrun.app/Contents/Resources/app_packages/aqt/speedrun/mcat_deck.apkg"

# 5. Package into a DMG
out/pyenv/bin/python qt/tools/build_installer.py --version 26.05 package
```

The DMG is written to `out/installer/dist/anki-26.05-mac-intel.dmg`. The app is
named **Speedrun** (set in `qt/installer/app/pyproject.toml`).

> **Stub binary note:** Briefcase currently downloads a Python 3.12 stub binary
> even though the bundled Python framework is 3.13. Step 3 replaces it with the
> correct Python 3.13 stub. This step is required on every fresh build.

> **Deck import format:** The bundled deck uses the newer `.anki21b` format
> (Zstandard-compressed). The auto-import in `_maybe_import_bundled_mcat_deck`
> uses Anki's Rust backend (`col.import_anki_package`) rather than the legacy
> Python importer, so both old and new `.apkg` formats are handled correctly.

> **Note on Gatekeeper:** The DMG is signed with an ad-hoc identity (no Apple
> Developer certificate). On another Mac, Gatekeeper will block it on first
> launch. To open it, right-click **Speedrun.app** → **Open** → **Open** in
> the dialog. This is a one-time step. Alternatively, reviewers can run the
> app directly from source with `just run` (no Gatekeeper prompt).

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
