#!/usr/bin/env python3
"""MCAT question generator using OpenAI GPT-4o and OpenStax textbooks.

Usage examples
--------------
Generate a new passage + 3 questions from an OpenStax excerpt:

    python speedrun/question_generator.py \\
        --topic Biochemistry \\
        --source-text "Enzymes lower activation energy by..." \\
        --source-citation "OpenStax Biochemistry, Chapter 6, https://openstax.org/..." \\
        --count 3

Generate questions from an *existing* passage (legacy mode):

    python speedrun/question_generator.py \\
        --topic Biology \\
        --passage "Researchers incubated cells in..." \\
        --source "OpenStax Biology 2e, Chapter 7" \\
        --count 4

Environment variables
---------------------
OPENAI_API_KEY   Required. Your OpenAI API key.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QUESTIONS_FILE = Path(__file__).parent / "questions.json"
GENERATED_FILE = Path(__file__).parent / "generated_questions.json"

OPENSTAX_SOURCES: dict[str, str] = {
    "Biochemistry": "OpenStax Biochemistry (https://openstax.org/details/books/biochemistry)",
    "Biology": "OpenStax Biology 2e (https://openstax.org/details/books/biology-2e)",
    "General-Chemistry": "OpenStax Chemistry 2e (https://openstax.org/details/books/chemistry-2e)",
    "Organic-Chemistry": "OpenStax Organic Chemistry (https://openstax.org/details/books/organic-chemistry)",
    "Physics-and-Math": "OpenStax University Physics Volume 1 (https://openstax.org/details/books/university-physics-volume-1)",
    "Behavioral": "OpenStax Psychology 2e (https://openstax.org/details/books/psychology-2e)",
    "CARS": "Project Gutenberg / open academic articles (https://www.gutenberg.org)",
}

SYSTEM_PROMPT = (
    "You are an expert MCAT question writer. You write passage-based multiple choice "
    "questions in the style of the AAMC MCAT exam. Every passage and question must be "
    "grounded in the source text provided. Do not introduce facts not present in the "
    "source text or standard scientific knowledge for the given topic."
)

# How long (seconds) to wait between API calls to stay within rate limits.
_INITIAL_BACKOFF = 1.0
_MAX_BACKOFF = 60.0
_MAX_RETRIES = 6

# Passage word-count targets.
_PASSAGE_MIN_WORDS = 150
_PASSAGE_MAX_WORDS = 250

# ---------------------------------------------------------------------------
# OpenAI helpers
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    """Load a .env file from the repo root into os.environ (no dependencies)."""
    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def _openai_client():  # type: ignore[return]
    """Return an openai.OpenAI client, raising clearly if the key is missing."""
    _load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print(
            "ERROR: OPENAI_API_KEY environment variable is not set.\n"
            "Export your key before running:\n"
            "  export OPENAI_API_KEY=sk-...",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        import openai  # type: ignore[import-not-found]
    except ImportError:
        print(
            "ERROR: openai package is not installed.\n"
            "Install it with:  pip install openai",
            file=sys.stderr,
        )
        sys.exit(1)
    return openai.OpenAI(api_key=api_key)


def _chat(client, messages: list[dict], model: str = "gpt-4o") -> str:
    """Call the OpenAI chat API with exponential backoff on rate-limit errors."""
    backoff = _INITIAL_BACKOFF
    for attempt in range(_MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.7,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
            is_rate_limit = "rate_limit" in err.lower() or "429" in err
            is_last = attempt == _MAX_RETRIES - 1
            if is_last or not is_rate_limit:
                raise
            wait = min(backoff * (2**attempt) + random.uniform(0, 1), _MAX_BACKOFF)
            print(
                f"Rate limit hit (attempt {attempt + 1}/{_MAX_RETRIES}). "
                f"Retrying in {wait:.1f}s…",
                file=sys.stderr,
            )
            time.sleep(wait)
    raise RuntimeError("Exhausted retries")  # unreachable


# ---------------------------------------------------------------------------
# Example loading
# ---------------------------------------------------------------------------


def _load_examples(n: int, topic: str | None = None) -> list[dict]:
    """Return up to *n* non-held-out questions from questions.json as style examples.

    Only questions with held_out=False (or no held_out field) are used — the
    held-out set is reserved exclusively for eval scoring.
    """
    try:
        from .eval import load_style_examples  # type: ignore[import]

        return load_style_examples(topic, n)
    except ImportError:
        pass
    # Fallback if eval module unavailable: filter manually
    if not QUESTIONS_FILE.exists():
        return []
    data = json.loads(QUESTIONS_FILE.read_text(encoding="utf-8"))
    all_qs: list[dict] = data.get("questions", [])
    training = [q for q in all_qs if not q.get("held_out", False)]
    if not training:
        training = all_qs
    matching = [q for q in training if q.get("topic") == topic] if topic else []
    others = [q for q in training if q not in matching]
    return (matching + others)[:n]


def _examples_block(examples: list[dict]) -> str:
    """Render example questions as indented JSON for the system prompt."""
    if not examples:
        return ""
    lines = ["Here are example MCAT questions showing the correct format, difficulty"]
    lines.append("level, and style. Match this format exactly:\n")
    lines.append(json.dumps(examples, indent=2, ensure_ascii=False))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Passage-id generation
# ---------------------------------------------------------------------------


def _next_passage_id(topic: str, existing: list[dict]) -> str:
    """Return the next auto-incremented passage_id for a generated passage."""
    slug = topic.lower().replace("-", "_").replace(" ", "_")
    prefix = f"{slug}_generated_"
    nums = []
    for q in existing:
        pid = q.get("passage_id", "")
        if pid.startswith(prefix):
            tail = pid[len(prefix) :]
            if tail.isdigit():
                nums.append(int(tail))
    next_num = (max(nums) + 1) if nums else 1
    return f"{prefix}{next_num:03d}"


def _next_question_id(topic: str, existing: list[dict]) -> str:
    """Return the next auto-incremented question id (e.g. biochemistry_gen_007)."""
    slug = topic.lower().replace("-", "_").replace(" ", "_")
    prefix = f"{slug}_gen_"
    nums = []
    for q in existing:
        qid = q.get("id", "")
        if qid.startswith(prefix):
            tail = qid[len(prefix) :]
            if tail.isdigit():
                nums.append(int(tail))
    next_num = (max(nums) + 1) if nums else 1
    return prefix + f"{next_num:03d}"


# ---------------------------------------------------------------------------
# Generation prompts
# ---------------------------------------------------------------------------

_PASSAGE_SCHEMA = """
Return a single JSON object (no markdown fences) with this exact schema:
{
  "passage": "<150-250 word MCAT-style passage describing an experiment or phenomenon>"
}
""".strip()

_QUESTIONS_SCHEMA = """
Return a JSON array (no markdown fences) of question objects.  Each object must
have exactly these fields, in this order:
{
  "question": "<question stem>",
  "choices": ["...", "...", "...", "..."],
  "correct_answer": "<A|B|C|D>",
  "concept": "<specific MCAT concept tested>",
  "rationale": "<why correct answer is right AND why each wrong answer is wrong>"
}
""".strip()


def _build_passage_messages(
    source_text: str,
    source_citation: str,
    topic: str,
    examples: list[dict],
) -> list[dict]:
    example_passages = list(
        dict.fromkeys(q["passage"] for q in examples if q.get("passage"))
    )[:2]
    example_block = ""
    if example_passages:
        joined = "\n\n---\n\n".join(example_passages)
        example_block = (
            f"\nHere are example MCAT passages for style reference "
            f"(match length and tone):\n\n{joined}\n"
        )

    user_content = (
        f"Topic (MCAT section): {topic}\n"
        f"Source citation: {source_citation}\n\n"
        f"Source text:\n{source_text}\n"
        f"{example_block}\n"
        f"Generate a 150-250 word MCAT-style passage grounded in this source text. "
        f"Describe an experiment or phenomenon with enough specific detail that "
        f"multiple questions can reference it.\n\n"
        f"{_PASSAGE_SCHEMA}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _build_questions_messages(
    passage: str,
    source_text: str,
    source_citation: str,
    topic: str,
    count: int,
    examples: list[dict],
) -> list[dict]:
    example_block = _examples_block(examples)
    user_content = (
        f"Topic (MCAT section): {topic}\n"
        f"Source citation: {source_citation}\n\n"
        f"Passage:\n{passage}\n\n"
        f"Source text (factual basis):\n{source_text}\n\n"
        f"{example_block}\n\n"
        f"Generate exactly {count} MCAT-style passage-based questions from the "
        f"passage above. Each question must require both passage reasoning AND "
        f"outside knowledge to answer — just like real MCAT questions. "
        f"Do not make questions answerable from the passage alone.\n\n"
        f"{_QUESTIONS_SCHEMA}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def _build_existing_passage_messages(
    passage: str,
    source: str,
    topic: str,
    count: int,
    examples: list[dict],
) -> list[dict]:
    """Legacy mode: generate questions from a caller-supplied passage."""
    example_block = _examples_block(examples)
    user_content = (
        f"Topic (MCAT section): {topic}\n"
        f"Source: {source}\n\n"
        f"Passage:\n{passage}\n\n"
        f"{example_block}\n\n"
        f"Generate exactly {count} MCAT-style passage-based questions from the "
        f"passage above. Each question must require both passage reasoning AND "
        f"outside knowledge.\n\n"
        f"{_QUESTIONS_SCHEMA}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> str:
    """Strip markdown code fences if the model included them."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ```
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_passage_response(raw: str) -> str:
    data = json.loads(_extract_json(raw))
    return data["passage"].strip()


_CHOICE_PREFIX = re.compile(r"^[A-Da-d][.)]\s+")


def _strip_choice_prefixes(choices: list[str]) -> list[str]:
    """Remove leading letter labels like 'A. ' or 'B) ' from answer choices."""
    return [_CHOICE_PREFIX.sub("", c) for c in choices]


def _parse_questions_response(raw: str) -> list[dict]:
    data = json.loads(_extract_json(raw))
    if isinstance(data, dict) and "questions" in data:
        data = data["questions"]
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array, got: {type(data)}")
    return data


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_generated() -> list[dict]:
    if not GENERATED_FILE.exists():
        return []
    data = json.loads(GENERATED_FILE.read_text(encoding="utf-8"))
    return data.get("questions", [])


def _save_generated(questions: list[dict]) -> None:
    existing = _load_generated()
    merged = existing + questions
    GENERATED_FILE.write_text(
        json.dumps({"questions": merged}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Core generation logic
# ---------------------------------------------------------------------------


def generate_from_source(
    *,
    client,
    topic: str,
    source_text: str,
    source_citation: str,
    count: int,
    num_examples: int,
) -> list[dict]:
    """Generate a new passage and *count* questions from an OpenStax excerpt."""
    existing_generated = _load_generated()
    examples = _load_examples(num_examples, topic)

    # --- Part 1: generate passage ---
    print("Generating passage…")
    passage_msgs = _build_passage_messages(
        source_text, source_citation, topic, examples
    )
    raw_passage = _chat(client, passage_msgs)
    passage = _parse_passage_response(raw_passage)
    word_count = len(passage.split())
    print(f"  Passage generated ({word_count} words).")

    # --- Part 2: generate questions ---
    print(f"Generating {count} question(s)…")
    q_msgs = _build_questions_messages(
        passage, source_text, source_citation, topic, count, examples
    )
    raw_qs = _chat(client, q_msgs)
    raw_items = _parse_questions_response(raw_qs)

    passage_id = _next_passage_id(topic, existing_generated)

    results: list[dict] = []
    for item in raw_items[:count]:
        qid = _next_question_id(topic, existing_generated + results)
        q: dict = {
            "id": qid,
            "passage_id": passage_id,
            "topic": topic,
            "generated_by": "openai-gpt-4o",
            "source_citation": source_citation,
            "source_text": source_text,
            "format_reference": "AAMC Sample Questions via speedrun/questions.json",
            "passage": passage,
        }
        q["question"] = item["question"]
        q["choices"] = _strip_choice_prefixes(item["choices"])
        q["correct_answer"] = item["correct_answer"]
        q["concept"] = item.get("concept", "").lower()
        q["rationale"] = item.get("rationale", "")
        results.append(q)

    return results


def generate_from_existing_passage(
    *,
    client,
    topic: str,
    passage: str,
    source: str,
    count: int,
    num_examples: int,
) -> list[dict]:
    """Legacy mode: generate questions from a caller-supplied passage."""
    existing_generated = _load_generated()
    examples = _load_examples(num_examples, topic)

    print(f"Generating {count} question(s) from existing passage…")
    msgs = _build_existing_passage_messages(passage, source, topic, count, examples)
    raw_qs = _chat(client, msgs)
    raw_items = _parse_questions_response(raw_qs)

    passage_id = _next_passage_id(topic, existing_generated)

    results: list[dict] = []
    for item in raw_items[:count]:
        qid = _next_question_id(topic, existing_generated + results)
        q: dict = {
            "id": qid,
            "passage_id": passage_id,
            "topic": topic,
            "generated_by": "openai-gpt-4o",
            "source": source,
            "format_reference": "AAMC Sample Questions via speedrun/questions.json",
            "passage": passage,
        }
        q["question"] = item["question"]
        q["choices"] = _strip_choice_prefixes(item["choices"])
        q["correct_answer"] = item["correct_answer"]
        q["concept"] = item.get("concept", "").lower()
        q["rationale"] = item.get("rationale", "")
        results.append(q)

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate MCAT-style questions using OpenAI GPT-4o.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # New-passage mode
    source_group = parser.add_argument_group("New-passage mode (OpenStax source)")
    source_group.add_argument(
        "--source-text",
        metavar="TEXT",
        help="Raw paragraph(s) copied from an OpenStax textbook.",
    )
    source_group.add_argument(
        "--source-citation",
        metavar="CITATION",
        help=(
            "Full OpenStax citation including book title, chapter, and URL. "
            "If omitted and --topic is given, the recommended OpenStax source "
            "for that topic is used as a placeholder."
        ),
    )

    # Legacy / existing-passage mode
    legacy_group = parser.add_argument_group("Existing-passage mode (legacy)")
    legacy_group.add_argument(
        "--passage",
        metavar="TEXT",
        help="Existing MCAT-style passage to generate questions from.",
    )
    legacy_group.add_argument(
        "--source",
        metavar="SOURCE",
        help="Source label for the existing passage.",
    )

    # Shared
    parser.add_argument(
        "--topic",
        required=True,
        choices=list(OPENSTAX_SOURCES.keys()),
        help="MCAT section / topic.",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=3,
        metavar="N",
        help="Number of questions to generate (default: 3).",
    )
    parser.add_argument(
        "--num-examples",
        type=int,
        default=3,
        metavar="N",
        help="Number of example questions from questions.json to use as style "
        "references (default: 3).",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o",
        help="OpenAI model to use (default: gpt-4o).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the prompts that would be sent without calling the API.",
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="Skip the automatic eval step and save all generated questions as-is.",
    )
    parser.add_argument(
        "--eval-verbose",
        action="store_true",
        help="Print per-question eval details.",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Validate mode
    new_passage_mode = bool(args.source_text)
    legacy_mode = bool(args.passage)

    if new_passage_mode and legacy_mode:
        parser.error("Use either --source-text or --passage, not both.")
    if not new_passage_mode and not legacy_mode:
        parser.error(
            "Provide either --source-text (new passage) or --passage (existing)."
        )

    # Fill in default citation
    if new_passage_mode and not args.source_citation:
        args.source_citation = OPENSTAX_SOURCES[args.topic]
        print(f"No --source-citation provided; using default: {args.source_citation}")

    client = _openai_client()

    if args.dry_run:
        examples = _load_examples(args.num_examples, args.topic)
        if new_passage_mode:
            msgs = _build_passage_messages(
                args.source_text, args.source_citation, args.topic, examples
            )
            print("=== PASSAGE PROMPT ===")
            for m in msgs:
                print(f"[{m['role']}]\n{m['content']}\n")
            q_msgs = _build_questions_messages(
                "<passage would be here>",
                args.source_text,
                args.source_citation,
                args.topic,
                args.count,
                examples,
            )
            print("=== QUESTIONS PROMPT ===")
            for m in q_msgs:
                print(f"[{m['role']}]\n{m['content']}\n")
        else:
            msgs = _build_existing_passage_messages(
                args.passage, args.source or "", args.topic, args.count, examples
            )
            print("=== QUESTIONS PROMPT ===")
            for m in msgs:
                print(f"[{m['role']}]\n{m['content']}\n")
        return

    if new_passage_mode:
        questions = generate_from_source(
            client=client,
            topic=args.topic,
            source_text=args.source_text,
            source_citation=args.source_citation,
            count=args.count,
            num_examples=args.num_examples,
        )
    else:
        questions = generate_from_existing_passage(
            client=client,
            topic=args.topic,
            passage=args.passage,
            source=args.source or "",
            count=args.count,
            num_examples=args.num_examples,
        )

    passage_id = questions[0]["passage_id"] if questions else "—"
    citation = args.source_citation if new_passage_mode else (args.source or "—")

    # --- Automatic eval ---
    if args.skip_eval:
        _save_generated(questions)
        print(
            f"\n✓ Generated {len(questions)} question(s) (eval skipped).\n"
            f"  passage_id : {passage_id}\n"
            f"  topic      : {args.topic}\n"
            f"  source     : {citation}\n"
            f"  saved to   : {GENERATED_FILE}"
        )
        return

    try:
        from .eval import (  # type: ignore[import]
            print_summary,
            run_eval,
            save_results,
        )
        from .eval import (
            save_generated as eval_save_generated,
        )
    except ImportError:
        # eval module not available — save without eval
        print("WARNING: eval module not found; saving without eval.", file=sys.stderr)
        _save_generated(questions)
        return

    try:
        annotated, summary = run_eval(
            questions, client, model=args.model, verbose=args.eval_verbose
        )
    except Exception as exc:  # noqa: BLE001
        # API failure during eval — save without eval rather than losing work
        print(
            f"WARNING: Eval failed ({exc}); saving questions without eval scores.",
            file=sys.stderr,
        )
        _save_generated(questions)
        return

    # Merge annotated questions into the full generated set and save
    existing = _load_generated()
    annotated_by_id = {q["id"]: q for q in annotated}
    merged = [annotated_by_id.get(q["id"], q) for q in existing]
    # Add any that are brand new (not yet in the file)
    existing_ids = {q["id"] for q in existing}
    for q in annotated:
        if q["id"] not in existing_ids:
            merged.append(q)
    eval_save_generated(merged)

    save_results(summary, passage_id=passage_id)
    print_summary(summary)

    n_passed = summary["questions_passed"]
    print(
        f"\n✓ Generated {len(questions)} question(s), "
        f"{n_passed} passed eval (score ≥ {summary['cutoff']}).\n"
        f"  passage_id : {passage_id}\n"
        f"  topic      : {args.topic}\n"
        f"  source     : {citation}\n"
        f"  saved to   : {GENERATED_FILE}"
    )


if __name__ == "__main__":
    main()
