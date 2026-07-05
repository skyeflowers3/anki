# Speedrun: MCAT Study Blocks

**An MCAT preparation app** forked from [Anki](https://github.com/ankitects/anki).

> This repository is a fork of Anki, which is © Ankitects Pty Ltd and contributors, licensed under the [GNU AGPL v3 or later](./LICENSE). All modifications in this fork are released under the same license (AGPL-3.0-or-later). See [Attribution](#attribution) below.

---

Speedrun keeps Anki's FSRS scheduler and card storage, but layers an MCAT-focused study experience on top: a points-at-stake review queue, per-section memory and performance scores, and an adaptive study loop that interleaves flashcards with AI-generated practice questions based on where your recall and application diverge.

## Project Documentation

| Document | Description |
|----------|-------------|
| [Architecture overview](speedrun/ARCHITECTURE.md) | How the desktop app, mobile app, adaptive loop, databases, AI generator, and sync system fit together |
| [Rust change note](speedrun/RUST_CHANGE.md) | The Points-at-Stake review ordering added to the Rust scheduler — what, why, files touched |
| [Model descriptions](speedrun/MODELS.md) | Memory, performance, and readiness models with formulas and give-up rules |
| [Files touched](speedrun/FILES_TOUCHED.md) | Every file modified or created in this fork, grouped by category |
| [Results report](speedrun/results_report.md) | Calibration results and honest interpretation |
| [Study test design](speedrun/STUDY_TEST.md) | Simulated ablation study for the two-step concept recognition feature |
| [speedrun/ README](speedrun/README.md) | CLI usage, re-running calibration tests |

## What Speedrun adds

- **Points-at-stake review queue** — due cards are ordered by `section weight × (1 − FSRS retrievability)`, descending, so high-value weak topics surface first. Implemented in the Rust scheduler (see [`RUST_CHANGE.md`](speedrun/RUST_CHANGE.md)).
- **Memory score** — reads FSRS retrievability (R) per card via Anki's own `extract_fsrs_retrievability` SQL function, aggregates it into the three MCAT sections (B/B, C/P, P/S), and shows a range. A give-up rule requires a minimum number of reviewed cards per subdeck before a section reports a score.
- **Performance score** — tracks practice-question accuracy (`correct / answered`) per topic, persisted to a `speedrun_performance` table inside the collection's SQLite database.
- **Readiness score** — projects a 472–528 MCAT composite from per-section accuracy with a 0.92 calibration factor and a binomial confidence interval.
- **Three-mode adaptive study loop** — interleaves flashcard blocks and question blocks, weighting each block by points-at-stake and switching modes (discovery → remediation → consolidation) based on the gap between a topic's memory and performance scores.
- **Two-step concept-recognition questions** — students identify the underlying concept (free-response, AI-graded by GPT-4o) before selecting the MC answer, with per-step feedback and an inline explanation card on mistakes.
- **AI question generation** — GPT-4o generates MCAT-style questions from OpenStax source content; a separate eval pass filters out low-quality questions before they enter the pool.
- **Firestore sync** — performance records and the question pool sync to Cloud Firestore so data is consistent across desktop and mobile devices.

## Layout

```
qt/aqt/speedrun/          ← integrated Speedrun Qt package (main code)
  driver.py               ← Qt controller; bridges Python ↔ JS quiz UI
  speedrun_loop.py        ← three-mode adaptive loop (block planning, routing)
  memory_score.py         ← FSRS-based memory score per topic/section
  performance_score.py    ← quiz accuracy score + question-block HTML/JS UI
  readiness_score.py      ← projected MCAT composite (472–528)
  auto_generator.py       ← background AI generation trigger
  question_generator.py   ← GPT-4o question generator (OpenStax → questions)
  eval.py                 ← question quality eval / filter
  question_sync.py        ← Firestore sync for questions + performance records
  openstax_fetcher.py     ← fetches OpenStax source content for generation
  questions.json          ← curated hand-written question bank
  generated_questions.json← AI-generated questions (eval-passed)

speedrun/                 ← standalone CLI copies + all project documentation
  ARCHITECTURE.md         ← full system architecture overview
  RUST_CHANGE.md          ← Rust scheduler change note
  MODELS.md               ← model descriptions with formulas
  FILES_TOUCHED.md        ← every file changed in this fork
  firebase/               ← Firestore project config and security rules

qt/aqt/stats.py           ← Stats UI, adds Memory / Performance / Readiness tabs
qt/aqt/overview.py        ← triggers the Speedrun loop on AnKing-MCAT study
rslib/src/scheduler/      ← points-at-stake queue ordering (Rust)
```

Everything else is upstream Anki (`rslib/`, `pylib/`, `qt/`, `ts/`, `proto/`).

---

## Building the Desktop App (macOS / Windows / Linux)

### Prerequisites

- **Rustup** — <https://rustup.rs/>. The version pinned in `rust-toolchain.toml` is downloaded automatically.
- **N2 or Ninja** — install N2 with `tools/install-n2` (recommended), or put Ninja 1.10+ on your `PATH`.
- **Python 3.9+** (64-bit). 3.9 is the best-tested version.
- **[just](https://just.systems/)** — `brew install just` or `uv tool install just`.

Platform-specific setup: `docs/windows.md`, `docs/mac.md`, `docs/linux.md`.

### Environment variables

Create a `.env` file at the repo root:

```
OPENAI_API_KEY=sk-...          # required for AI question generation + grading
FIREBASE_PROJECT_ID=...        # required for Firestore sync
FIREBASE_API_KEY=...           # required for Firestore sync
SPEEDRUN_SYNC_ID=...           # auto-generated on first run; use the same value
                               # on every device you want to share data across
```

### Run and build

```bash
just run              # build pylib + qt and launch Speedrun (debug)
just run-optimized    # release-optimized build
just check            # format + full build + lint + all tests
```

Web views are served at <http://localhost:40000/_anki/pages/> during development. Run `just web-watch` in a separate terminal for live-reloading web assets.

### Tests

```bash
just test        # Rust, Python, and TypeScript tests
just test-rust   # Rust only
just test-py     # Python only
just test-ts     # TypeScript/Svelte (Vitest)
just test-e2e    # Playwright browser end-to-end tests
```

The points-at-stake ordering has Python-level coverage in `pylib/tests/test_schedv3.py`.

### Installer

Speedrun uses Anki's Briefcase-based installer (`qt/installer/`). Build locally:

```bash
just wheels
out/pyenv/bin/python qt/tools/build_installer.py build \
    --version "$(cat .version)" \
    --anki_wheel <path-to-anki-wheel> \
    --aqt_wheel <path-to-aqt-wheel>
```

Produces a `.app`/`.dmg` on macOS, MSI on Windows, `.tar.zst` on Linux.

---

## Building the Android App (AnkiDroid fork)

The Android companion app lives at [skyeflowers3/Anki-Android](https://github.com/skyeflowers3/Anki-Android).

### Prerequisites

- Android Studio (Hedgehog or later) with NDK installed
- JDK 17+

### Environment variables

Add to `local.properties` at the repo root (same directory as `build.gradle`):

```
speedrunSyncId=<your SPEEDRUN_SYNC_ID from the desktop .env>
speedrunApiKey=<your FIREBASE_API_KEY>
speedrunProjectId=brillianter-app
```

These are injected into `BuildConfig` at build time. Without them, Firestore sync is silently disabled.

### Build

```bash
./gradlew :AnkiDroid:assembleDebug     # debug APK
./gradlew :AnkiDroid:assembleRelease   # release APK (requires signing config)
```

Or open the project in Android Studio and run it directly on a device or emulator.

---

## Known limitations

- **Deck-name coupling** — the section/topic mapping is hardcoded to AnKing-MCAT subdeck names. Decks named differently won't roll up into B/B, C/P, and P/S sections.
- **Performance give-up threshold** — sections need ≥ 30 answered questions total (and ≥ 10 per topic for 3-topic sections) before showing a score.
- **Fork maintenance** — Speedrun modifies upstream files (`qt/aqt/stats.py`, the Rust scheduler); pulling upstream Anki changes may require manual merges.

---

## Attribution

Speedrun is a fork of **Anki**, © Ankitects Pty Ltd and contributors.

- Upstream repository: <https://github.com/ankitects/anki>
- Upstream website: <https://apps.ankiweb.net>
- License: [GNU Affero General Public License v3 or later (AGPL-3.0-or-later)](./LICENSE)

All modifications made in this fork are released under the same AGPL-3.0-or-later license. The FSRS scheduling algorithm, card storage engine, and all other upstream components remain the work of the Anki contributors. Speedrun's contribution is the adaptive study loop, scoring models, AI question generation pipeline, and Firestore sync layer built on top of that foundation.
