# Speedrun Model Descriptions

---

## 1. Memory Model

**What it measures**

The memory score estimates how well a student has retained the MCAT flashcard material in long-term memory right now. It is derived directly from FSRS (Free Spaced Repetition Scheduler) retrievability — the probability, as estimated by the scheduling algorithm, that the student can correctly recall a given card at the moment of measurement.

**How it is calculated**

FSRS maintains two parameters per card: *stability* S (the interval in days at which retrievability equals 90%) and *difficulty* D. Current retrievability is:

```
R(t) = 0.9 ^ (t / S)
```

where t is the number of days elapsed since the card was last reviewed. R = 1.0 immediately after a correct review and decays toward 0 as time passes. R = 0.9 exactly when the card is due.

Cards are grouped into MCAT sections by subdeck:

| Section | Subdecks |
|---------|----------|
| B/B — Biological & Biochemical Foundations | Biology, Biochemistry, Essential-Equations |
| C/P — Chemical & Physical Foundations | General-Chemistry, Organic-Chemistry, Physics-and-Math |
| P/S — Psychological, Social & Biological Foundations | Behavioral |

The section score is the arithmetic mean of R across all cards in that section's subdecks. Cards that have never been reviewed (no FSRS state) are excluded from the mean.

**Output range:** 0–100%. A score of 85% means the student is expected to correctly recall 85% of that section's cards right now.

**Give-up rule**

A section score is only displayed when the student has reviewed a minimum number of cards, to prevent a misleading score from a thin sample:

- Every section requires ≥ **30 total reviewed cards** across its subdecks.
- Sections with 3 subdecks (B/B and C/P) additionally require ≥ **10 reviewed cards in each subdeck**, so one heavily-reviewed deck cannot mask gaps in another.
- P/S (single subdeck) only needs the 30-total rule.

Until the threshold is met the section shows "not enough data" and lists which subdecks still need reviews.

---

## 2. Performance Model

**What it measures**

The performance score measures accuracy on MCAT-style multiple-choice practice questions. Unlike the memory score, which is based on flashcard recall, the performance score reflects how well the student applies knowledge to exam-format questions — the same format used on the real MCAT.

**How it is calculated**

Every answered question is stored in a `speedrun_performance` table with a binary `answer_correct` flag (1 = correct, 0 = incorrect). The concept free-response is evaluated separately for feedback purposes and does not affect the accuracy percentage.

Questions are organized by topic and roll up into sections using the same mapping as the memory model, plus CARS (which has questions but no flashcard deck):

| Section | Topics |
|---------|--------|
| B/B | Biology, Biochemistry, Essential-Equations |
| C/P | General-Chemistry, Organic-Chemistry, Physics-and-Math |
| P/S | Behavioral |
| CARS | CARS |

Section accuracy is a flat sum — not an average of per-topic averages — so larger topics receive proportionally more weight:

```
section accuracy = sum(answer_correct across all topics) / count(all rows in section)
```

**Output range:** 0–100%. A score of 74% means the student answered 74% of that section's practice questions correctly across all sessions.

**Give-up rule**

A section score is only displayed once the student has answered a minimum number of questions, mirroring the memory model's approach:

- Every section requires ≥ **30 total answered questions**.
- Sections with ≥ 3 topics (B/B and C/P) additionally require ≥ **10 answered questions per topic**, so a topic with very few answers cannot disproportionately skew the section score.
- P/S and CARS (single-topic sections) only need the 30-total rule.

Until the threshold is met the section shows "X more needed" and lists which topics are still short.

---

## 3. Readiness Model

**What it measures**

The readiness score is a projected MCAT composite score on the official 472–528 scale, derived entirely from performance-score accuracy across all four MCAT sections. It translates the student's question-answering accuracy into the same numeric range as a real MCAT result, including a confidence interval that shrinks as more questions are answered.

**How it is calculated**

Each MCAT section is scored 118–132 (a range of 14 points). Random guessing on a 4-choice question corresponds to 25% accuracy and maps to the section floor (118). A calibration factor of **0.92** is applied to account for the fact that AI-generated practice questions tend to be slightly easier than the real exam:

```
eff   = section_accuracy × 0.92
ratio = clamp((eff − 0.25) / 0.75, 0, 1)
score = 118 + ratio × 14
```

The four section scores are summed to produce the projected composite:

```
projected = round(B/B score + C/P score + P/S score + CARS score)
```

**Confidence interval**

Per-section uncertainty is derived from the binomial sampling variance of accuracy and propagated through the linear score formula. Assuming the four sections are independent, the errors are combined in quadrature:

```
se_section = sqrt(p × (1−p) / n) / 0.75 × 14 × 0.92
total_se   = sqrt(se_B/B² + se_C/P² + se_P/S² + se_CARS²)
low, high  = round(projected ± 2 × total_se)   ← ≈ 95% confidence interval
```

A confidence label is also shown based on total questions answered across all sections: **low** (< 100), **medium** (100–299), **high** (≥ 300).

**Output range:** 472–528 (projected composite), with a ± uncertainty range. Example: "507 (499–515, medium confidence)".

**Give-up rule**

The readiness score is not shown until **every section has passed its own performance-score threshold** (≥ 30 answered, ≥ 10 per topic for B/B and C/P). Until then the display lists which sections are still blocked and how many more questions each one needs. This prevents a misleading composite score from being shown while large portions of the exam are untested.
