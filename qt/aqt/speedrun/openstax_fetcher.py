"""
openstax_fetcher.py — download and cache one chapter section per MCAT topic.

Usage (library):
    from speedrun.openstax_fetcher import fetch_topic
    result = fetch_topic("Biochemistry")
    if result:
        clean_text, citation = result

Each call downloads a CNXML (or HTML for CARS) file from a known URL, saves it
to speedrun/openstax_cache/, and returns 200-250 words of clean body text plus
a formatted citation string.  On subsequent calls the cached file is read
instead of re-downloading.

Returns None if download or parse fails — never raises.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CACHE_DIR = Path(__file__).resolve().parent / "openstax_cache"

_RAW_BASE = "https://raw.githubusercontent.com/openstax/{repo}/main/modules/{module}/index.cnxml"

# Topic metadata: repo, module_id, book_name, chapter_title, canonical_url
# Each entry maps an MCAT topic name to the upstream source.
TOPIC_MAP: dict[str, dict] = {
    "Biochemistry": {
        "type": "cnxml",
        "repo": "osbooks-biology-bundle",
        "module": "m62763",
        "book": "Biology 2e",
        "chapter": "Energy and Metabolism",
        "url": "https://openstax.org/books/biology-2e/pages/6-1-energy-and-metabolism",
        "cache_file": "Biochemistry.xml",
    },
    "Biology": {
        "type": "cnxml",
        "repo": "osbooks-biology-bundle",
        "module": "m62787",
        "book": "Biology 2e",
        "chapter": "Glycolysis",
        "url": "https://openstax.org/books/biology-2e/pages/7-2-glycolysis",
        "cache_file": "Biology.xml",
    },
    "General-Chemistry": {
        "type": "cnxml",
        "repo": "osbooks-chemistry-bundle",
        "module": "m68786",
        "book": "Chemistry 2e",
        "chapter": "Chemical Reaction Rates",
        "url": "https://openstax.org/books/chemistry-2e/pages/12-1-chemical-reaction-rates",
        "cache_file": "General-Chemistry.xml",
    },
    "Organic-Chemistry": {
        "type": "cnxml",
        "repo": "osbooks-organic-chemistry",
        "module": "m00158",
        "book": "Organic Chemistry",
        "chapter": "Atomic Structure: The Nucleus",
        "url": "https://openstax.org/books/organic-chemistry/pages/1-2-atomic-structure-the-nucleus",
        "cache_file": "Organic-Chemistry.xml",
    },
    "Physics-and-Math": {
        "type": "cnxml",
        "repo": "osbooks-university-physics-bundle",
        "module": "m58719",
        "book": "University Physics Volume 1",
        "chapter": "Work",
        "url": "https://openstax.org/books/university-physics-volume-1/pages/7-1-work",
        "cache_file": "Physics-and-Math.xml",
    },
    "Behavioral": {
        "type": "cnxml",
        "repo": "osbooks-psychology",
        "module": "m82172",
        "book": "Psychology 2e",
        "chapter": "Human Genetics",
        "url": "https://openstax.org/books/psychology-2e/pages/3-1-human-genetics",
        "cache_file": "Behavioral.xml",
    },
    "CARS": {
        "type": "html",
        "source_url": "https://plato.stanford.edu/entries/consciousness/",
        "book": "Stanford Encyclopedia of Philosophy",
        "chapter": "Consciousness",
        "url": "https://plato.stanford.edu/entries/consciousness/",
        "cache_file": "CARS.html",
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _download(url: str) -> Optional[bytes]:
    """Download raw bytes from *url*.  Returns None on any failure."""
    try:
        import ssl
        import urllib.request

        # Build an SSL context that trusts the system's cert store, and falls
        # back to unverified if certs are missing (common on macOS dev installs).
        try:
            import certifi  # type: ignore[import-not-found]
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.create_default_context()
            # If the system store is unavailable, fall back to no-verify so
            # the fetcher can still run in development environments.
            try:
                ctx.load_default_certs()
            except Exception:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(url, headers={"User-Agent": "speedrun-fetcher/1.0"})
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return resp.read()
    except Exception:
        return None


def _extract_cnxml_text(xml_bytes: bytes, word_target: int = 225) -> Optional[str]:
    """
    Parse a CNXML file and extract plain body text.

    Skips: metadata, figures, captions, math (m:math / MathML), tables,
    exercises, and any section titled 'Teacher Support', 'Learning Objectives',
    'Connection for AP', 'Everyday Connection', 'Science Practice', or
    'Section Summary'.

    Returns word_target ± 25 words.
    """
    SKIP_TITLES = {
        "teacher support", "learning objectives", "connection for ap",
        "everyday connection", "science practice connection for ap",
        "ap courses", "science practice", "section summary",
        "link to learning", "review questions", "critical thinking",
        "test prep", "outcomes of glycolysis", "teacher support",
    }
    CNXML_NS = "http://cnx.rice.edu/cnxml"

    try:
        # Strip namespace declarations that might trip up ElementTree
        xml_text = xml_bytes.decode("utf-8", errors="replace")
        # Remove MathML blocks wholesale before parsing
        xml_text = re.sub(r"<m:math[\s\S]*?</m:math>", " ", xml_text)
        xml_text = re.sub(r"<math[\s\S]*?</math>", " ", xml_text)

        root = ET.fromstring(xml_text)

        words: list[str] = []

        def _skip_elem(elem: ET.Element) -> bool:
            """Return True if this element should be entirely skipped."""
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag in ("figure", "caption", "media", "image", "table", "tgroup",
                       "metadata", "glossary", "note", "exercise", "problem",
                       "solution", "commentary", "list"):
                return True
            # Skip note/section elements whose title matches skip list
            title_el = elem.find(f"{{{CNXML_NS}}}title")
            if title_el is not None and title_el.text:
                if title_el.text.strip().lower() in SKIP_TITLES:
                    return True
            return False

        def _gather(elem: ET.Element) -> None:
            if len(words) >= word_target + 25:
                return
            if _skip_elem(elem):
                return
            # Collect inline text
            if elem.text:
                chunk = elem.text.strip()
                if chunk:
                    words.extend(chunk.split())
            for child in elem:
                _gather(child)
                if child.tail:
                    chunk = child.tail.strip()
                    if chunk:
                        words.extend(chunk.split())

        _gather(root)

        if len(words) < 50:
            return None

        # Trim to target window (200-250 words)
        trimmed = words[: word_target + 25]
        # Back off to last sentence boundary near the target
        text = " ".join(trimmed)
        # Find a sentence break near word_target
        approx = " ".join(words[:word_target])
        last_period = max(approx.rfind(". "), approx.rfind("? "), approx.rfind("! "))
        if last_period > len(approx) * 0.6:
            text = approx[: last_period + 1].strip()
        else:
            text = approx.strip()

        # Clean up stray whitespace / XML artifacts
        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text.split()) >= 50 else None

    except Exception:
        return None


def _extract_html_text(html_bytes: bytes, word_target: int = 225) -> Optional[str]:
    """
    Extract plain text from a Stanford Encyclopedia of Philosophy HTML page.
    Targets the <div id="main-text"> section, stripping tags/scripts/math.
    """
    try:
        html = html_bytes.decode("utf-8", errors="replace")

        # Pull the main article body
        main_match = re.search(
            r'<div[^>]+id=["\']main-text["\'][^>]*>([\s\S]*?)</div>\s*</div>\s*</div>',
            html,
        )
        if main_match:
            body = main_match.group(1)
        else:
            # Fallback: content between first <p> and 5000 chars
            p_match = re.search(r"<p[^>]*>([\s\S]{500,5000})", html)
            body = p_match.group(0) if p_match else html

        # Remove scripts, styles, footnotes, navigation elements
        body = re.sub(r"<script[\s\S]*?</script>", "", body)
        body = re.sub(r"<style[\s\S]*?</style>", "", body)
        body = re.sub(r"<sup[^>]*>[\s\S]*?</sup>", "", body)
        body = re.sub(r"<div[^>]+class=['\"][^'\"]*toc[^'\"]*['\"][\s\S]*?</div>", "", body)
        # Strip all remaining HTML tags
        body = re.sub(r"<[^>]+>", " ", body)
        # Decode common HTML entities
        body = body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        body = body.replace("&#160;", " ").replace("&nbsp;", " ")
        body = body.replace("&#8220;", '"').replace("&#8221;", '"')
        body = body.replace("&#8216;", "'").replace("&#8217;", "'")
        body = re.sub(r"&#\d+;", "", body)
        body = re.sub(r"&\w+;", "", body)
        body = re.sub(r"\s+", " ", body).strip()

        words = body.split()
        if len(words) < 50:
            return None

        approx = " ".join(words[:word_target])
        last_period = max(approx.rfind(". "), approx.rfind("? "), approx.rfind("! "))
        if last_period > len(approx) * 0.5:
            text = approx[: last_period + 1].strip()
        else:
            text = approx.strip()

        text = re.sub(r"\s+", " ", text).strip()
        return text if len(text.split()) >= 50 else None

    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_topic(topic: str) -> Optional[tuple[str, str]]:
    """
    Return *(clean_text, citation)* for *topic*, or *None* on failure.

    Files are cached under speedrun/openstax_cache/ and never re-downloaded.

    Citation format:
      - Science topics: "OpenStax <Book>, <Chapter>, <URL>"
      - CARS: "Stanford Encyclopedia of Philosophy, Consciousness, <URL>"
    """
    meta = TOPIC_MAP.get(topic)
    if meta is None:
        return None

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / meta["cache_file"]
    citation = _build_citation(meta)

    # --- Read from cache if present ---
    if cache_path.exists():
        raw = cache_path.read_bytes()
    else:
        # --- Download ---
        if meta["type"] == "cnxml":
            url = _RAW_BASE.format(repo=meta["repo"], module=meta["module"])
        else:
            url = meta["source_url"]
        raw = _download(url)
        if raw is None:
            return None
        cache_path.write_bytes(raw)

    # --- Parse ---
    if meta["type"] == "cnxml":
        text = _extract_cnxml_text(raw)
    else:
        text = _extract_html_text(raw)

    if text is None:
        return None
    return text, citation


def _build_citation(meta: dict) -> str:
    if meta.get("type") == "html":
        return f"Stanford Encyclopedia of Philosophy, {meta['chapter']}, {meta['url']}"
    return f"OpenStax {meta['book']}, {meta['chapter']}, {meta['url']}"


def available_topics() -> list[str]:
    """Return the list of topics supported by this fetcher."""
    return list(TOPIC_MAP.keys())


# ---------------------------------------------------------------------------
# CLI — python -m speedrun.openstax_fetcher [topic ...]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch and cache OpenStax chapter text.")
    parser.add_argument(
        "topics",
        nargs="*",
        default=list(TOPIC_MAP.keys()),
        help="Topics to fetch (default: all)",
    )
    parser.add_argument("--no-cache", action="store_true", help="Re-download even if cached")
    args = parser.parse_args()

    if args.no_cache:
        for topic in args.topics:
            meta = TOPIC_MAP.get(topic)
            if meta:
                path = CACHE_DIR / meta["cache_file"]
                if path.exists():
                    path.unlink()
                    print(f"Cleared cache for {topic}")

    for topic in args.topics:
        print(f"\n{'='*60}")
        print(f"Topic: {topic}")
        result = fetch_topic(topic)
        if result is None:
            print("  [FAILED — download or parse error]")
        else:
            text, citation = result
            word_count = len(text.split())
            print(f"  Citation : {citation}")
            print(f"  Words    : {word_count}")
            print(f"  Preview  : {text[:200]}...")
