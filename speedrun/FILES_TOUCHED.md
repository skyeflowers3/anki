# Files Touched in This Anki Fork

All changes are relative to the upstream Anki desktop repository. Files are grouped by category. "A" = added, "M" = modified.

---

## New Speedrun files — `qt/aqt/speedrun/`

The core Speedrun package, imported by the running desktop app.

| Status | File | Description |
|--------|------|-------------|
| A | `__init__.py` | Package init; re-exports `SpeedrunController` and `maybe_start` |
| A | `driver.py` | Qt driver — wires the adaptive loop to the UI, handles all `pycmd` callbacks from JavaScript, manages state transitions between flashcard and question blocks; also contains `PracticeQuizController` for the standalone 20–60 question practice mode with subject filtering, background AI generation, and a "← Back" button that returns to the MCAT home screen; quiz rendering reuses `render_question_block()` + the existing `srq:` bridge protocol from study blocks rather than a custom JS template |
| A | `home.py` | MCAT home screen — custom `mcatHome` main-window state that replaces the deck browser at startup; shows time-of-day greeting, Start Study Session and Practice Quiz action buttons, live per-section memory+performance score table, and a "View decks" link |
| A | `speedrun_loop.py` | Pure decision logic — which block to serve next, mode transitions (discovery → remediation → consolidation), points-at-stake weighting; no Qt imports so it is unit-testable in isolation |
| A | `memory_score.py` | Reads FSRS retrievability from the collection via `extract_fsrs_retrievability`, aggregates by MCAT section, renders the Memory Score Stats tab |
| A | `performance_score.py` | Reads `speedrun_performance` table, tallies quiz accuracy per topic/section, renders the quiz UI (passage, concept input, multiple-choice, feedback), renders the Performance Score Stats tab; treats Essential-Equations as an optional/supplemental topic that does not gate section readiness |
| A | `readiness_score.py` | Projects a 472–528 MCAT composite from per-section accuracy with a 0.92 calibration factor and binomial confidence interval; renders the Readiness Score Stats tab; shows a recommendation banner if Essential-Equations deck is missing (but does not block the score) |
| A | `coverage_map.py` | Tracks whether the app covers all official MCAT content areas; distinguishes required areas (block readiness score if missing) from recommended areas (show tip only); renders the MCAT Coverage Stats tab |
| A | `question_sync.py` | Firestore REST sync — pushes performance records with deduplication via `sync_key`, pulls AI-generated questions, collection-group pull across all device sync IDs, offline outbox queue for failed pushes |
| A | `auto_generator.py` | Background thread that triggers AI question generation when a topic's question bank falls below the minimum threshold |
| A | `question_generator.py` | CLI — generates new MCAT-style questions from OpenStax source text via GPT-4o |
| A | `eval.py` | CLI — filters AI-generated questions against a gold standard via GPT-4o before they enter the question bank |
| A | `openstax_fetcher.py` | Fetches and caches OpenStax HTML/XML source content for the question generator |
| A | `questions.json` | Curated hand-written question bank |
| A | `generated_questions.json` | AI-generated questions that have passed eval |
| A | `eval_results.json` | Output of the most recent eval run |
| A | `calibrate_memory.py` | Calibration script — plots FSRS predicted R vs. actual recall rate on held-out reviews, prints Brier score |
| A | `calibrate_performance.py` | Calibration script — reports held-out accuracy (last 20% of answered questions) by MCAT section |
| A | `mcat_deck.apkg` | Bundled MCAT Study Blocks deck (224 MB) — auto-imported silently on first launch via `_maybe_import_bundled_mcat_deck()` in `main.py` |
| A | `openstax_cache/` | Cached OpenStax XML/HTML per topic (7 files) — avoids redundant network fetches during question generation |

---

## New Speedrun files — `speedrun/` (CLI / standalone sibling)

Standalone copies of the core modules for CLI use, development, and Firebase config.

| Status | File | Description |
|--------|------|-------------|
| A | `README.md` | CLI usage guide, project documentation index, re-running tests instructions |
| A | `ARCHITECTURE.md` | Overview of the full system: desktop app, mobile app, adaptive loop, databases, AI generator, sync |
| A | `RUST_CHANGE.md` | Focused note on the Points-at-Stake Rust scheduler change: what, why, files touched, how it fits the existing architecture |
| A | `MODELS.md` | One-page descriptions of the memory, performance, and readiness models with formulas and give-up rules |
| A | `STUDY_TEST.md` | Simulated ablation study design for the two-step concept recognition feature |
| A | `FILES_TOUCHED.md` | This file |
| A | `results_report.md` | Calibration results and honest interpretation for project submission |
| A | `proof/calibration_chart.png` | Memory model calibration chart output |
| A | `proof/calibration_performance.png` | Performance model calibration chart output |
| A | `memory_score.py` | CLI copy of the memory score module |
| A | `performance_score.py` | CLI copy of the performance score module |
| A | `speedrun_loop.py` | CLI copy of the adaptive loop decision logic |
| A | `question_generator.py` | CLI copy of the question generator |
| A | `auto_generator.py` | CLI copy of the background generator |
| A | `openstax_fetcher.py` | CLI copy of the OpenStax fetcher |
| A | `question_sync.py` | CLI copy of the Firestore sync helpers |
| A | `eval.py` | CLI copy of the eval filter |
| A | `questions.json` | CLI copy of the question bank |
| A | `generated_questions.json` | CLI copy of AI-generated questions |
| A | `eval_results.json` | CLI copy of eval results |
| A | `firebase/firebase.json` | Firebase project config |
| A | `firebase/firestore.rules` | Firestore security rules for the sync collections |

---

## Modified Anki desktop files — `qt/aqt/`

Changes to the existing Anki Qt application layer.

| Status | File | What was changed and why |
|--------|------|--------------------------|
| M | `overview.py` | `_linkHandler` routes to Speedrun adaptive loop via `maybe_start()`; "Practice Quiz" button removed (now lives on the home screen) |
| M | `stats.py` | Added four `QWebEngineView` tabs: MCAT Readiness (before Coverage), MCAT Coverage, MCAT Memory, MCAT Performance; tab order puts Readiness first |
| M | `webview.py` | Added `AnkiWebViewKind` enum values: `MCAT_MEMORY`, `MCAT_PERFORMANCE`, `MCAT_READINESS`, `MCAT_COVERAGE`, `SPEEDRUN_LOOP` |
| M | `main.py` | Window title override; `_maybe_import_bundled_mcat_deck()` auto-imports `mcat_deck.apkg` on first launch using the Rust backend (`col.import_anki_package`) so that the newer `.anki21b` / Zstandard-compressed format is handled correctly; `_deckBrowserState()` redirects startup `deckBrowser` transition to the MCAT home screen when the MCAT deck is present |
| M | `toolbar.py` | `_centerLinks()` prepends a "Study" link that navigates to the MCAT home screen whenever the MCAT Study Blocks deck exists; `_mcatHomeLinkHandler()` creates/reuses `McatHomeController` |
| M | `deckbrowser.py` | Full redesign: time-of-day greeting, "← Back to Study Home" button, deck table in a card-style container; `_go_mcat_home()` bridge handler for the back button |
| M | `data/web/css/deckbrowser.scss` | New styles for `#sr-home`, `#sr-greeting`, `#sr-salutation`, `#sr-tagline`, `#sr-deck-wrap`, `#sr-back-btn`, `#studiedToday` |

---

## Modified Anki desktop files — build system

| Status | File | What was changed and why |
|--------|------|--------------------------|
| M | `qt/pyproject.toml` | Changed `formal_name` to "Speedrun" for the installer; noted that hatchling auto-bundles JSON/XML data files under `aqt/speedrun/` |
| M | `qt/installer/app/pyproject.toml` | Updated installer app name to "Speedrun" |
| M | `qt/tools/build_installer.py` | Updated installer build script to reflect the Speedrun app name; removed `--update-support` flag so Briefcase reuses its local cache instead of forcing a re-download of the Python support package |
| M | `.gitignore` | Added ignore rules for `speedrun_outbox.json`, `.env`, OpenStax cache, and other generated artifacts |
| M | `.github/workflows/release.yml` | Updated release workflow for the Speedrun fork |
| M | `DECK_SETUP.md` | Added (root-level) setup instructions for importing the MCAT deck |

---

## Modified Anki desktop files — Rust scheduler

| Status | File | What was changed and why |
|--------|------|--------------------------|
| M | `proto/anki/deck_config.proto` | Added `REVIEW_CARD_ORDER_POINTS_AT_STAKE = 13` to the `ReviewCardOrder` enum — required to wire the new ordering through the protobuf API |
| M | `rslib/src/storage/card/mod.rs` | Added `SECTION_WEIGHTS` constant table mapping MCAT subdecks to exam-weight multipliers; added `ReviewOrderSubclause::PointsAtStake` with the SQL expression `section_weight × (1 − FSRS_retrievability)`; wired into `review_order_sql()` |
| M | `rslib/src/scheduler/queue/builder/mod.rs` | Added test helpers `add_review_card_with_memory()` and `points_at_stake_order()`; added `points_at_stake_generates_expected_sql` test |
| M | `rslib/src/scheduler/fsrs/simulator.rs` | Minor adjustment to the FSRS simulator to accommodate the new ordering mode |
| M | `pylib/tests/test_schedv3.py` | Added a Python-level integration test for the Points-at-Stake ordering |
| M | `ts/routes/deck-options/choices.ts` | Added the new enum value to the TypeScript deck-options ordering dropdown |
| M | `ftl/core/deck-config.ftl` | Added the display string for the Points-at-Stake ordering option |
| M | `docs/development.md` | Updated development notes to document the new review ordering |

---

## Modified AnkiDroid files

Changes to the AnkiDroid (Android) fork at [skyeflowers3/Anki-Android](https://github.com/skyeflowers3/Anki-Android).

| Status | File | What was changed and why |
|--------|------|--------------------------|
| A | `AnkiDroid/src/main/java/com/ichi2/anki/speedrun/SpeedrunDb.kt` | New file — SQLite helpers: `CREATE TABLE speedrun_performance`, `recordAnswer()` with fuzzy concept matching, `fetchTopicResults()` for cumulative accuracy, `sync_key` UUID generation for Firestore deduplication |
| A | `AnkiDroid/src/main/java/com/ichi2/anki/speedrun/SpeedrunQuestion.kt` | New file — data class and JSON deserializer for the question bank |
| A | `AnkiDroid/src/main/java/com/ichi2/anki/speedrun/StudyLoopActivity.kt` | New file — full study loop UI: passage display, concept input, multiple-choice, adaptive question ordering, session summary, performance and readiness score screens |
| A | `AnkiDroid/src/main/java/com/ichi2/anki/speedrun/SpeedrunFirestoreSync.kt` | New file — Firestore REST sync via OkHttp: pushes performance records after each answer, pulls remote records on session start, anonymous Firebase auth token cached for 55 minutes |
| A | `AnkiDroid/src/main/assets/speedrun/questions.json` | Bundled question bank served to the Android study loop |
| M | `AnkiDroid/src/main/java/com/ichi2/anki/DeckPicker.kt` | Added hook to launch `StudyLoopActivity` when the user taps to study the `AnKing-MCAT` deck |
| M | `AnkiDroid/AndroidManifest.xml` | Registered `StudyLoopActivity` |
| M | `AnkiDroid/build.gradle` | Added `speedrunSyncId` and `speedrunApiKey` BuildConfig injection from `local.properties` / environment variables; added OkHttp dependency |
