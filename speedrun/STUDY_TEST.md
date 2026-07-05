# Speedrun Feature Experiment: Two-Step Concept Recognition

> **Note:** The results in this document are **simulated**. This is a study design and proof-of-concept analysis using synthetic data. A real test would require multiple participants studying over days or weeks, with pre-registration and statistical power sufficient to detect a meaningful effect. The simulation below illustrates the experimental structure and demonstrates that the design is feasible.

---

## Pre-Registered Hypothesis

**H1:** Studying with two-step concept recognition will produce higher accuracy on held-out transfer questions than studying without it, at equal study time.

**Null hypothesis (H0):** Study condition has no effect on transfer accuracy.

**Primary metric:** Accuracy % on 20 held-out transfer questions (answer_correct / 20), identical across all three conditions.

**Pre-registered expected effect:** Full Speedrun will achieve ≥ 10 percentage points higher transfer accuracy than Ablation, and ≥ 20 percentage points higher than Plain Anki.

---

## Study Design

Each learner goes through two phases:

### Study phase — different per build (15 minutes)

The intervention is how the student studies, not what they are tested on.

| Build | Study method | What the student does during study |
|-------|-------------|-------------------------------------|
| **Full Speedrun** | Loop on, concept recognition **on** | Identifies the underlying concept before answering each practice question; both steps are graded and drive the adaptive loop |
| **Ablation** | Loop on, concept recognition **off** | Answers practice questions directly with the same adaptive loop, but no concept identification step |
| **Plain Anki** | Standard flashcard review | Reviews AnKing-MCAT flashcards in default Anki order; no practice questions |

### Test phase — same for all builds (no time limit)

After the study phase, every learner answers the **same 20 held-out questions** they have never seen before. These questions are drawn from topics covered during the study phase but were withheld from the question bank during studying. The test measures how well the study method transferred to new material — not how well the student performed while studying.

```
Study phase (15 min)        Test phase (same questions for everyone)
─────────────────────       ────────────────────────────────────────
Build 1: Full Speedrun  ─→
Build 2: Ablation       ─→  20 held-out questions → accuracy %
Build 3: Plain Anki     ─→
```

### Participants and order

- **3 learners**, within-subject (each does all three builds on separate days)
- **Topics:** B/B section (Biochemistry, Biology), held constant across all builds
- **Order counterbalanced** to control for learning and fatigue effects

| Learner | Build order |
|---------|-------------|
| L1 | Full Speedrun → Ablation → Plain Anki |
| L2 | Plain Anki → Full Speedrun → Ablation |
| L3 | Ablation → Plain Anki → Full Speedrun |

---

## Simulated Results

### Transfer accuracy on 20 held-out questions

| Learner | Full Speedrun | Ablation | Plain Anki |
|---------|:-------------:|:--------:|:----------:|
| L1 | 14 / 20 = **70.0%** | 11 / 20 = **55.0%** | 8 / 20 = **40.0%** |
| L2 | 13 / 20 = **65.0%** | 12 / 20 = **60.0%** | 9 / 20 = **45.0%** |
| L3 | 11 / 20 = **55.0%** | 11 / 20 = **55.0%** | 8 / 20 = **40.0%** |
| **Mean** | **63.3%** | **56.7%** | **41.7%** |
| **SD** | ±6.2% | ±2.9% | ±2.9% |

### What each learner studied during the study phase (for context)

| Learner | Full Speedrun — questions attempted | Ablation — questions attempted | Plain Anki — cards reviewed |
|---------|:-----------------------------------:|:------------------------------:|:---------------------------:|
| L1 | 18 | 22 | 34 |
| L2 | 16 | 21 | 31 |
| L3 | 19 | 20 | 36 |

*(Differences in questions attempted during study reflect pace variation, not a design choice. All builds were capped at 15 minutes.)*

---

## Interpretation

### What supported the hypothesis

For L1 and L2, studying with Full Speedrun produced clearly higher transfer accuracy than both the Ablation (+10–15pp) and Plain Anki (+20–25pp). The concept step appears to help students build a mental schema — associating the correct underlying principle with a question type — that transfers to new questions they haven't seen before.

The adaptive loop also adds independent value: the Ablation condition consistently outperformed Plain Anki (~15pp), suggesting that practicing with questions in any adaptive format is better preparation for a question-based test than flashcard review alone.

### The result that did not work

**L3 showed no benefit from the concept recognition step.** Their transfer accuracy was identical between Full Speedrun and Ablation (55% each), while both outperformed Plain Anki. One plausible explanation: L3's concept identification accuracy during the study phase was poor (~45%), meaning they were learning incorrect concept associations. When the concept step is unreliable, it may add noise rather than signal. This points to the importance of the memory-gating rule already in the Speedrun loop — a topic should not receive concept-graded question blocks until the student has reviewed enough flashcards to identify concepts reliably. L3's result suggests the eligibility thresholds (currently ≥ 30 reviewed cards AND memory > 75%) may need to be raised, or that the feedback on wrong concept responses needs to be stronger.

---

## Honest Caveats

1. **This data is simulated.** The numbers above were constructed to reflect plausible outcomes given the system's design, with realistic noise. They do not come from real study sessions.

2. **n = 3 is underpowered.** With three participants, no significance test is meaningful. A real study would require at least 20–30 participants to detect a 10-point accuracy difference with 80% power at α = 0.05.

3. **Within-subject order effects.** Even with counterbalancing, three participants cannot fully balance three conditions. Learning effects across days (getting better at MCAT questions regardless of build) could inflate later conditions.

4. **One day per build is too short.** MCAT preparation effects accumulate over weeks. A real experiment would have participants study for several days per build and measure transfer at the end of each study period.

5. **20 held-out questions is a small test set.** A 5-question swing (e.g. 11/20 vs. 12/20) changes the accuracy by 5pp. A larger test set (50–100 questions) would produce more stable estimates.

---

## What a Real Experiment Would Require

- ≥ 20 participants recruited and randomised before any data collection
- Pre-registration of the hypothesis, metric, and analysis plan (e.g. on OSF)
- Separate held-out question sets per study period, difficulty-matched across builds
- Multi-day study periods (e.g. 3 days per build) with transfer tests at the end of each
- A mixed-effects model to account for between-learner variance and order effects
- A washout period between builds to prevent carry-over learning
