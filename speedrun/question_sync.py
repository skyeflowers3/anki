"""Sync AI-generated questions to/from Firebase Firestore.

Pushes eval-passed questions to a Firestore collection after generation and
pulls them down at app startup, keeping the question pool consistent across
reinstalls and multiple machines.

Requires in .env (or environment):
    FIREBASE_PROJECT_ID=brillianter-app
    FIREBASE_API_KEY=<web-api-key>

All network calls are best-effort — a missing key or network error never
blocks or crashes a study session.

Public API
----------
push_question(question)          — call after a question passes eval
maybe_pull_into_local_cache()    — call on app start (run in a daemon thread)
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_log = logging.getLogger("speedrun.question_sync")

_GENERATED_PATH = Path(__file__).resolve().parent / "generated_questions.json"
_COLLECTION = "speedrun_questions"
_TIMEOUT = 10  # seconds per HTTP request
_PAGE_SIZE = 300

# Cached anonymous Firebase ID token and its expiry (Unix timestamp).
_id_token: str = ""
_token_expires: float = 0.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
    env_path = Path(__file__).parent.parent / ".env"
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


def _config() -> tuple[str, str] | None:
    """Return (project_id, api_key) or None when not configured."""
    _load_dotenv()
    project = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    key = os.environ.get("FIREBASE_API_KEY", "").strip()
    return (project, key) if (project and key) else None


def _base_url(project: str) -> str:
    return (
        f"https://firestore.googleapis.com/v1/projects/{project}"
        f"/databases/(default)/documents/{_COLLECTION}"
    )


# ---------------------------------------------------------------------------
# Firestore document encoding / decoding
# ---------------------------------------------------------------------------


def _to_firestore(q: dict) -> dict:
    """Convert a flat question dict to a Firestore document ``fields`` map."""

    def _encode(v: Any) -> dict:
        if isinstance(v, bool):
            return {"booleanValue": v}
        if isinstance(v, int):
            return {"integerValue": str(v)}
        if isinstance(v, float):
            return {"doubleValue": v}
        if isinstance(v, list):
            return {"arrayValue": {"values": [{"stringValue": str(i)} for i in v]}}
        if isinstance(v, dict):
            return {"stringValue": json.dumps(v, ensure_ascii=False)}
        return {"stringValue": str(v) if v is not None else ""}

    return {"fields": {k: _encode(v) for k, v in q.items()}}


def _from_firestore(doc: dict) -> dict:
    """Convert a Firestore document to a flat question dict."""
    out: dict = {}
    for k, v in doc.get("fields", {}).items():
        if "stringValue" in v:
            raw = v["stringValue"]
            try:
                decoded = json.loads(raw)
                if isinstance(decoded, dict):
                    out[k] = decoded
                    continue
            except (json.JSONDecodeError, ValueError):
                pass
            out[k] = raw
        elif "integerValue" in v:
            out[k] = int(v["integerValue"])
        elif "doubleValue" in v:
            out[k] = float(v["doubleValue"])
        elif "booleanValue" in v:
            out[k] = bool(v["booleanValue"])
        elif "arrayValue" in v:
            out[k] = [
                item.get("stringValue", "")
                for item in v["arrayValue"].get("values", [])
            ]
        else:
            out[k] = None
    return out


def _doc_id(question_id: str | int) -> str:
    """Sanitize a question ID to a valid Firestore document ID."""
    return str(question_id).replace("/", "_").replace(".", "_")[:1500]


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:  # noqa: BLE001
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _anon_token(api_key: str) -> str:
    """Return a cached Firebase anonymous ID token, refreshing when near expiry.

    Requires Anonymous Authentication to be enabled in the Firebase project:
    Firebase Console → Authentication → Sign-in methods → Anonymous → Enable
    """
    import time

    global _id_token, _token_expires
    if _id_token and time.time() < _token_expires - 60:
        return _id_token
    url = (
        f"https://identitytoolkit.googleapis.com/v1/accounts:signUp"
        f"?key={urllib.parse.quote(api_key, safe='')}"
    )
    body = json.dumps({"returnSecureToken": True}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            _id_token = data.get("idToken", "")
            expires_in = int(data.get("expiresIn", 3600))
            _token_expires = time.time() + expires_in
            return _id_token
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            _log.warning(
                "Firestore anonymous auth failed (HTTP 400). "
                "Enable Anonymous Authentication in the Firebase Console: "
                "https://console.firebase.google.com/project/brillianter-app"
                "/authentication/providers"
            )
        else:
            _log.debug("Anonymous auth failed: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        _log.debug("Anonymous auth failed: %s", exc)
        return ""


def _http(url: str, method: str = "GET", body: dict | None = None) -> dict | None:
    cfg = _config()
    token = _anon_token(cfg[1]) if cfg else ""
    data = json.dumps(body).encode() if body else None
    headers: dict[str, str] = {}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, context=_ssl_ctx(), timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        _log.debug("Firestore %s %s → HTTP %s", method, url.split("?")[0], exc.code)
        return None
    except Exception as exc:  # noqa: BLE001
        _log.debug("Firestore request failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def push_question(question: dict) -> None:
    """Write one approved question to Firestore. No-op if not configured."""
    cfg = _config()
    if not cfg:
        return
    project, key = cfg
    qid = question.get("id")
    if not qid:
        return
    url = (
        f"{_base_url(project)}/{_doc_id(qid)}"
        f"?key={urllib.parse.quote(key, safe='')}"
    )
    result = _http(url, method="PATCH", body=_to_firestore(question))
    if result:
        _log.info("Pushed question %r to Firestore.", qid)
    else:
        _log.debug("Failed to push question %r (will retry on next generation).", qid)


def pull_questions() -> list[dict]:
    """Fetch all questions from Firestore. Returns [] if unconfigured or on error."""
    cfg = _config()
    if not cfg:
        return []
    project, key = cfg
    questions: list[dict] = []
    page_token: str | None = None

    while True:
        params = f"key={urllib.parse.quote(key, safe='')}&pageSize={_PAGE_SIZE}"
        if page_token:
            params += f"&pageToken={urllib.parse.quote(page_token, safe='')}"
        result = _http(f"{_base_url(project)}?{params}")
        if not result:
            break
        for doc in result.get("documents", []):
            try:
                q = _from_firestore(doc)
                if q:
                    questions.append(q)
            except Exception:  # noqa: BLE001
                pass
        page_token = result.get("nextPageToken")
        if not page_token:
            break

    _log.info("Pulled %d question(s) from Firestore.", len(questions))
    return questions


def maybe_pull_into_local_cache() -> None:
    """Pull Firestore questions and merge new ones into generated_questions.json.

    Only adds questions with ``eval_passed: true`` that aren't already present
    locally — never overwrites existing local entries.
    Run this in a daemon thread; it blocks on network I/O.
    """
    try:
        remote = pull_questions()
        if not remote:
            return

        existing: list[dict] = []
        if _GENERATED_PATH.exists():
            try:
                data = json.loads(_GENERATED_PATH.read_text(encoding="utf-8"))
                existing = data.get("questions", [])
            except Exception:  # noqa: BLE001
                pass

        existing_ids = {q.get("id") for q in existing}
        added = 0
        for q in remote:
            if not q.get("eval_passed", False):
                continue
            if q.get("id") in existing_ids:
                continue
            existing.append(q)
            added += 1

        if added:
            _GENERATED_PATH.write_text(
                json.dumps({"questions": existing}, indent=2, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            _log.info(
                "Merged %d new question(s) from Firestore into local cache.", added
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("maybe_pull_into_local_cache failed: %s", exc)
