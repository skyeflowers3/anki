# Speedrun Architecture

Speedrun is an MCAT adaptive learning system built on top of two Anki forks: a macOS desktop fork and an AnkiDroid (Android) fork. Both apps share the same question bank, the same scoring formulas, and the same Firestore sync layer.

---

## Desktop App (macOS Anki fork)

### Entry point

The desktop app is a fork of the open-source Anki desktop application. Speedrun intercepts the standard "Study" button for the top-level `AnKing-MCAT` deck in `qt/aqt/overview.py`:

```python
# qt/aqt/overview.py
from aqt.speedrun import maybe_start
if maybe_start(self.mw, deck):
    return False   # Speedrun takes over; skip standard review
```

`maybe_start` is defined in `qt/aqt/speedrun/driver.py`. It starts the adaptive loop for the top-level deck and falls through to standard Anki review for individual subdecks, so the rest of the app is unaffected.

### Custom code location

All Speedrun-specific code lives in `qt/aqt/speedrun/`:

| File | Role |
|------|------|
| `driver.py` | Qt driver — wires loop decisions to the UI, handles `pycmd` calls from JavaScript, manages state transitions |
| `speedrun_loop.py` | Pure decision logic — which block to serve next, mode transitions, points-at-stake weighting (no Qt imports) |
| `memory_score.py` | Reads FSRS retrievability from the collection and aggregates it by MCAT section |
| `performance_score.py` | Reads `speedrun_performance` table, tallies quiz accuracy per topic/section, renders the quiz UI |
| `readiness_score.py` | Projects a 472–528 MCAT composite score from performance accuracy |
| `question_sync.py` | Firestore REST sync: pushes performance records, pulls AI questions, offline outbox queue |
| `auto_generator.py` | Background thread that triggers AI question generation when the question bank is thin |
| `question_generator.py` | CLI: generates new questions via OpenAI GPT-4o from OpenStax source text |
| `eval.py` | CLI: filters AI-generated questions against a gold standard before they enter the question bank |
| `openstax_fetcher.py` | Fetches and caches OpenStax HTML/XML source for the question generator |

### Stats tab

`qt/aqt/stats.py` is modified to add three new tabs to Anki's Stats dialog:

- **MCAT Memory** — renders `memory_score.render_html(col)`, showing FSRS retrievability by section
- **MCAT Performance** — renders `performance_score.render_html(col)`, showing quiz accuracy by section
- **MCAT Readiness** — renders `readiness_score.render_html(col)`, showing the projected composite score

Each tab uses an `AnkiWebView` (`webview.py` has three new `AnkiWebViewKind` enum values: `MCAT_MEMORY`, `MCAT_PERFORMANCE`, `MCAT_READINESS`).

### Rust scheduler change

A new review card ordering mode — **Points-at-Stake** — was added to the Rust scheduler. See [`RUST_CHANGE.md`](RUST_CHANGE.md) for details.

---

## Mobile App (AnkiDroid fork)

The mobile app is a fork of the open-source AnkiDroid Android application. All Speedrun-specific code is isolated in a new package:

```
AnkiDroid/src/main/java/com/ichi2/anki/speedrun/
    SpeedrunDb.kt          — SQLite helpers: CREATE TABLE, recordAnswer(), fetchTopicResults()
    SpeedrunQuestion.kt    — Data class + JSON deserializer for the question bank
    StudyLoopActivity.kt   — Full study loop UI: passages, multiple-choice, concept input,
                             session summary, performance/readiness score screens
```

**Hook into DeckPicker.kt:** When the user taps to study the `AnKing-MCAT` deck, `DeckPicker.kt` launches `StudyLoopActivity` instead of the standard reviewer.

**Question bank:** `AnkiDroid/src/main/assets/speedrun/questions.json` is bundled in the APK.

**Firestore sync:** `SpeedrunFirestoreSync.kt` pushes every answered question to Firestore via plain OkHttp REST calls (no Firebase Android SDK). It also pulls remote records on session start. The sync identity (`SPEEDRUN_SYNC_ID`) and API key are injected at build time from `local.properties` or the `SPEEDRUN_SYNC_ID` environment variable.

**Concept grading:** Concepts are graded offline using fuzzy word-overlap matching (normalise to lowercase, strip punctuation, check that all words from the correct concept are present in the student's answer). This approximates the desktop's GPT-4o semantic grading without requiring a network call.

---

## Adaptive Loop

The loop is implemented in `qt/aqt/speedrun/speedrun_loop.py` (pure Python, no Qt) and driven by `qt/aqt/speedrun/driver.py` on the desktop.

**Three modes:**

1. **Interleaved discovery (Mode 1):** Mixed-topic question blocks, weighted by points-at-stake (section weight × weakness). After each block, check every topic for a knowledge gap; if found, switch to Mode 2.
2. **Focused remediation (Mode 2):** Flashcard and question blocks focused on the single weakest topic until its block pattern is solid (mostly concept-right/answer-right) or a flashcard block leaves it eligible and above 65% performance.
3. **Interleaved consolidation (Mode 3):** One mixed block that includes the freshly remediated topic, then return to Mode 1.

**Two input signals** (both read from the live collection each block):
- **Memory:** FSRS retrievability per topic from `memory_score.py`
- **Performance:** Quiz accuracy per topic from `performance_score.py`

A topic must have ≥ 20 reviewed flashcards AND memory > 75% before it is eligible for question blocks. Below either bar it receives flashcard blocks only.

---

## Databases

### Desktop — `speedrun_performance` table

Stored inside the Anki collection (`collection.anki2`, SQLite). Created on demand by `performance_score.ensure_table()`.

Key columns: `answered_at`, `question_id`, `topic`, `concept`, `chosen_concept`, `correct_concept`, `chosen_answer`, `correct_answer`, `concept_correct`, `answer_correct`, `sync_key`.

The `sync_key` (UUID4 hex) is the Firestore document ID for deduplication across devices.

### Mobile — `ankidroid_speedrun.db`

A separate SQLite database stored in the Android app's private data directory. Schema matches the desktop table (same column names, same types) so Firestore records are interchangeable.

---

## AI Question Generator

`question_generator.py` generates new MCAT-style multiple-choice questions using GPT-4o:

1. `openstax_fetcher.py` fetches and caches relevant OpenStax textbook sections as source material.
2. GPT-4o is prompted to write a passage, question, four answer choices, the correct answer, and the underlying concept.
3. `eval.py` filters generated questions against a gold standard (also via GPT-4o) before they are added to `generated_questions.json`.
4. `auto_generator.py` runs this pipeline in a background thread when the question bank for a topic has fewer than a configurable minimum.

Questions marked `"eval_passed": true` in `generated_questions.json` are served to students. Manual questions in `questions.json` are served only when no AI-generated questions exist for a topic yet.

---

## Sync System

All sync runs over the Firestore REST API (no Firebase SDK on either platform). Anonymous authentication uses the Firebase Identity Toolkit (`accounts:signUp`), with the token cached for 55 minutes.

**Performance records:** Pushed to `speedrun_performance/{SYNC_ID}/records/{sync_key}` after every answered question. The desktop pulls using a Firestore **collection group query** (`allDescendants: true`) so it aggregates records from all device sync IDs in one request. An **offline outbox** (`speedrun_outbox.json`) buffers any failed pushes and retries them at the start of the next session.

**AI questions:** Pulled from a shared `speedrun_questions` Firestore collection so all devices receive new questions as they pass eval, without requiring an app update.
