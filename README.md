# Speedrun: MCAT Study Blocks

Speedrun is an MCAT study app forked from [Anki](https://apps.ankiweb.net). It
keeps Anki's FSRS scheduler and card storage, but layers an MCAT-focused study
experience on top: a points-at-stake review queue, per-section memory and
performance scores, and an adaptive study loop that interleaves flashcards with
practice questions based on where your recall and application diverge.

## What Speedrun adds

- **Points-at-stake review queue** — due cards are ordered by
  `section weight × (1 − FSRS retrievability)`, descending, so high-value weak
  topics surface first instead of plain due order. Implemented in the Rust
  scheduler (see commit `d4a21dfef`).
- **Memory score** — reads FSRS retrievability (R) per card straight from the
  collection (via Anki's own `extract_fsrs_retrievability` SQL function),
  aggregates it into the three MCAT sections (B/B, C/P, P/S), and shows a range.
  A give-up rule requires a minimum number of reviewed cards per subdeck before
  a section reports a score.
- **Performance score** — tracks practice-question accuracy
  (`correct / answered`) per topic, persisted to a `speedrun_performance` table
  inside the collection's SQLite database so results survive between sessions.
- **Three-mode adaptive study loop** — interleaves flashcard blocks and question
  blocks, weighting each block by points-at-stake and switching modes based on
  the gap between a topic's memory score and its performance score.
- **Two-step concept-recognition questions** — students identify the underlying
  concept before answering the question itself.
- **Custom UI** — integrated into Anki's Stats screen and deck overview, plus a
  "Speedrun: MCAT Study Blocks" banner on the deck list and window title.

## Layout

- `speedrun/` — the custom, mostly self-contained logic:
  - `memory_score.py` — FSRS-based memory score (usable as a CLI or imported).
  - `performance_score.py` — practice-question accuracy scoring.
  - `speedrun_loop.py` — the three-mode adaptive study loop.
  - `questions.json` — the practice question bank.
- `qt/aqt/stats.py` — imports the `speedrun` modules to render the memory and
  performance scores in the desktop Stats UI.
- `rslib/src/scheduler/` — the points-at-stake queue ordering (Rust).

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
`pylib/tests/test_schedv3.py` (added in commit `d4a21dfef`).

## Building the installer

Speedrun uses Anki's Briefcase-based installer (templates in `qt/installer/`).

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
- **Small demo question bank** — `questions.json` is a demo set, and the
  performance give-up threshold (`MIN_ANSWERED`) is only 3. A real question bank
  should raise it to 10–15 for meaningful accuracy.
- **Memory-score CLI reads a copy** — the CLI opens a temporary copy of the
  collection so it never locks or mutates live data; numbers can lag the live
  collection if it changed since the copy.
- **Give-up rule can withhold scores** — sections/topics without enough reviewed
  cards or answered questions show "not enough data" rather than a score.
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
