# Speedrun: MCAT Study Blocks

Speedrun is an MCAT study app forked from [Anki](https://apps.ankiweb.net). It
keeps Anki's FSRS scheduler and card storage, but layers an MCAT-focused study
experience on top: a points-at-stake review queue, per-section memory and
performance scores, and an adaptive study loop that interleaves flashcards with
AI-generated practice questions based on where your recall and application
diverge.

## What Speedrun adds

- **Points-at-stake review queue** — due cards are ordered by
  `section weight × (1 − FSRS retrievability)`, descending, so high-value weak
  topics surface first instead of plain due order. Implemented in the Rust
  scheduler (see [Rust change commit](#rust-change-commit)).
- **Memory score** — reads FSRS retrievability (R) per card straight from the
  collection (via Anki's own `extract_fsrs_retrievability` SQL function),
  aggregates it into the three MCAT sections (B/B, C/P, P/S), and shows a
  range. A give-up rule requires a minimum number of reviewed cards per subdeck
  before a section reports a score.
- **Performance score** — tracks practice-question accuracy
  (`correct / answered`) per topic, persisted to a `speedrun_performance` table
  inside the collection's SQLite database so results survive between sessions.
- **Readiness score** — combines memory and performance into a single
  section-level readiness signal, shown in the Stats UI alongside the other
  scores.
- **Three-mode adaptive study loop** — interleaves flashcard blocks and
  question blocks, weighting each block by points-at-stake and switching modes
  based on the gap between a topic's memory score and its performance score.
- **Two-step concept-recognition questions** — students identify the underlying
  concept (free-response, AI-graded by GPT-4o) before selecting the MC answer,
  with per-step feedback and an inline explanation card on mistakes.
- **AI question generation** — GPT-4o generates MCAT-style questions from
  OpenStax source content; a separate eval pass filters out low-quality
  questions before they enter the pool. Generated questions are stored in
  `qt/aqt/speedrun/generated_questions.json`.
- **Firestore sync** — practice-question records and the question pool are
  synced to Cloud Firestore so performance data is consistent across devices
  sharing the same `SPEEDRUN_SYNC_ID`. Sync is incremental on startup (only
  fetches records newer than the local maximum).
- **Custom UI** — integrated into Anki's Stats screen and deck overview, plus a
  "Speedrun: MCAT Study Blocks" banner on the deck list and window title.

## Layout

```
qt/aqt/speedrun/          ← integrated Speedrun Qt package (main code)
  driver.py               ← Qt controller; bridges Python ↔ JS quiz UI
  speedrun_loop.py        ← three-mode adaptive loop (block planning, routing)
  memory_score.py         ← FSRS-based memory score per topic/section
  performance_score.py    ← quiz accuracy score + question-block HTML/JS UI
  readiness_score.py      ← combined readiness score (memory × performance)
  auto_generator.py       ← background AI generation trigger
  question_generator.py   ← GPT-4o question generator (OpenStax → questions)
  eval.py                 ← question quality eval / filter
  question_sync.py        ← Firestore sync for questions + performance records
  openstax_fetcher.py     ← fetches OpenStax source content for generation
  questions.json          ← curated hand-written question bank
  generated_questions.json← AI-generated questions (eval-passed)
  eval_results.json       ← latest eval run output (pass/fail per question)
  openstax_cache/         ← cached OpenStax HTML/XML (fetched once, reused)
  DECK_SETUP.md           → see DECK_SETUP.md at repo root

speedrun/                 ← standalone CLI copies of core modules
  firebase/               ← Firestore project config and security rules
  (see speedrun/README.md for details)

qt/aqt/stats.py           ← Stats UI, imports the three score modules
qt/aqt/overview.py        ← triggers the Speedrun loop on AnKing-MCAT study
qt/aqt/deckbrowser.py     ← "Speedrun: MCAT Study Blocks" deck-list banner
rslib/src/scheduler/      ← points-at-stake queue ordering (Rust)
```

Everything else is upstream Anki (`rslib/`, `pylib/`, `qt/`, `ts/`, `proto/`).

## Prerequisites

Building from source requires the same toolchain as upstream Anki:

- **Rustup** — <https://rustup.rs/>. The version pinned in `rust-toolchain.toml`
  is downloaded automatically.
- **N2 or Ninja** — install N2 with `tools/install-n2` (recommended for better
  status output), or put Ninja 1.10+ on your `PATH`.
- **Python 3.9+** (64-bit). 3.9 is the best-tested version.
- **[just](https://just.systems/)** command runner — `brew install just` or
  `uv tool install just`. All project commands are exposed as `just` recipes.

Platform-specific setup is documented in `docs/windows.md`, `docs/mac.md`, and
`docs/linux.md`. Run `just --list` to see every available recipe.

## Environment variables

Create a `.env` file at the repo root (see `.env.example` if present) with:

```
OPENAI_API_KEY=sk-...          # required for AI question generation + grading
FIREBASE_PROJECT_ID=...        # required for Firestore sync
FIREBASE_API_KEY=...           # required for Firestore sync
SPEEDRUN_SYNC_ID=...           # auto-generated on first run; set the same
                               # value on every device you want to share
                               # performance data across
```

## Building and running

```bash
just run              # build pylib + qt and launch Speedrun (debug)
just run-optimized    # same, release-optimized build
just build            # build without launching
```

Web views are served at <http://localhost:40000/_anki/pages/> during
development. For live-reloading web assets, run `just web-watch` in a separate
terminal (or `just rebuild-web` for a one-off rebuild).

## Running tests

```bash
just check       # format + full build + lint + all tests (run before committing)
just test        # Rust, Python, and TypeScript tests
just test-rust   # Rust only
just test-py     # Python only (pylib + qt)
just test-ts     # TypeScript/Svelte (Vitest)
just test-e2e    # Playwright browser end-to-end tests
```

The points-at-stake ordering has Python-level coverage in
`pylib/tests/test_schedv3.py` (added in the Rust change commit below).

## Building the installer

Speedrun uses Anki's Briefcase-based installer (templates in `qt/installer/`).
See [DECK_SETUP.md](./DECK_SETUP.md) for how to bundle the MCAT deck.

- **Locally**, build the wheels first, then drive the installer script for the
  current platform:

  ```bash
  just wheels
  out/pyenv/bin/python qt/tools/build_installer.py build \
      --version "$(cat .version)" \
      --anki_wheel <path-to-anki-wheel> \
      --aqt_wheel <path-to-aqt-wheel>
  ```

  The script wraps `briefcase build` and produces a platform-appropriate bundle
  (`.app`/`.dmg` on macOS, MSI on Windows, `.tar.zst` on Linux).

- **Via CI**, dispatch the release workflow (builds all platforms, no signing or
  publishing):

  ```bash
  just release build --ref <branch-or-tag>
  ```

The current version is read from the `.version` file (currently `26.05`).

## Known limitations

- **Deck-name coupling** — the section/topic mapping is hardcoded to
  AnKing-MCAT subdeck names (Biology, Biochemistry, General-Chemistry,
  Organic-Chemistry, Physics-and-Math, Behavioral, Essential-Equations). Decks
  named differently won't roll up into the B/B, C/P, and P/S sections.
- **Performance give-up threshold** — sections need ≥ 30 answered questions
  total (and ≥ 10 per topic for 3-topic sections) before showing a score.
  Topics below threshold are weighted more aggressively in question selection to
  accelerate coverage.
- **Memory-score CLI reads a copy** — the CLI opens a temporary copy of the
  collection so it never locks or mutates live data; numbers can lag the live
  collection if it changed since the copy.
- **Fork maintenance** — Speedrun modifies upstream files (`qt/aqt/stats.py`,
  the Rust scheduler); pulling upstream Anki changes may require manual merges.

## Rust change commit

The points-at-stake review ordering lives in commit **`d4a21dfef`**
("Add points-at-stake review order (MCAT section weight x FSRS weakness)"),
touching `rslib/src/scheduler/queue/builder/mod.rs` and
`rslib/src/storage/card/mod.rs` among others.

## Attribution

Speedrun is a fork of Anki, which is licensed under the
[GNU AGPL v3](./LICENSE). See the upstream project at
<https://apps.ankiweb.net> and <https://github.com/ankitects/anki>.
