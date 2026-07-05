"""Sync AI-generated questions and performance records to/from Firebase Firestore.

Pushes eval-passed questions to a Firestore collection after generation and
pulls them down at app startup, keeping the question pool consistent across
reinstalls and multiple machines.

Also syncs answer records so performance scores and adaptive ordering are
identical on every device sharing the same SPEEDRUN_SYNC_ID.

Requires in .env (or environment):
    FIREBASE_PROJECT_ID=brillianter-app
    FIREBASE_API_KEY=<web-api-key>
    SPEEDRUN_SYNC_ID=<shared UUID — same on every device you want to sync>

SPEEDRUN_SYNC_ID is auto-generated on first run and written back to .env if
the file exists, or stored only in memory otherwise.

All network calls are best-effort — a missing key or network error never
blocks or crashes a study session.

Public API
----------
push_question(question)                  — call after a question passes eval
maybe_pull_into_local_cache()            — call on app start (run in a daemon thread)
push_performance_record(record)          — call after each quiz answer
maybe_sync_performance(col)              — call at Speedrun session start
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_log = logging.getLogger("speedrun.question_sync")

_GENERATED_PATH = Path(__file__).resolve().parent / "generated_questions.json"
_OUTBOX_PATH = Path(__file__).resolve().parent / "speedrun_outbox.json"
_COLLECTION = "speedrun_questions"
_PERF_COLLECTION = "speedrun_performance"
_TIMEOUT = 10  # seconds per HTTP request
_PAGE_SIZE = 300

# Cached anonymous Firebase ID token and its expiry (Unix timestamp).
# Persisted to disk so restarts skip the re-auth round-trip.
_id_token: str = ""
_token_expires: float = 0.0
_TOKEN_CACHE_FILE = Path(__file__).resolve().parent / ".firebase_token_cache.json"

# Cached sync identity — generated once per process, persisted to .env.
_sync_id: str = ""

# Protects concurrent reads/writes to the outbox file from multiple threads.
_outbox_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _load_dotenv() -> None:
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


def _perf_base_url(project: str, sync_id: str) -> str:
    return (
        f"https://firestore.googleapis.com/v1/projects/{project}"
        f"/databases/(default)/documents/{_PERF_COLLECTION}"
        f"/{sync_id}/records"
    )


def _get_sync_id() -> str:
    """Return a persistent SPEEDRUN_SYNC_ID, generating one on first call.

    The ID is read from the environment / .env file.  If absent it is
    generated as a UUID4 hex string and written back to .env (if that file
    exists) so it survives restarts.  On all devices that should share
    performance data, set SPEEDRUN_SYNC_ID to the same value in .env.
    """
    import uuid

    global _sync_id
    if _sync_id:
        return _sync_id

    _load_dotenv()
    existing = os.environ.get("SPEEDRUN_SYNC_ID", "").strip()
    if existing:
        _sync_id = existing
        return _sync_id

    # Generate a new ID and try to persist it.
    new_id = uuid.uuid4().hex
    _sync_id = new_id
    os.environ["SPEEDRUN_SYNC_ID"] = new_id

    env_path = Path(__file__).parent.parent.parent.parent / ".env"
    if env_path.exists():
        try:
            text = env_path.read_text(encoding="utf-8")
            text = text.rstrip("\n") + f"\nSPEEDRUN_SYNC_ID={new_id}\n"
            env_path.write_text(text, encoding="utf-8")
            _log.info("Generated new SPEEDRUN_SYNC_ID=%s and saved to .env.", new_id)
        except Exception:  # noqa: BLE001
            _log.info("Generated new SPEEDRUN_SYNC_ID=%s (in-memory only).", new_id)
    else:
        _log.info(
            "Generated SPEEDRUN_SYNC_ID=%s (no .env found; set this in .env on "
            "all devices you want to sync).",
            new_id,
        )
    return _sync_id


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

    Token is persisted to disk so restarts skip re-authentication.
    Requires Anonymous Authentication to be enabled in the Firebase project:
    Firebase Console → Authentication → Sign-in methods → Anonymous → Enable
    """
    import time

    global _id_token, _token_expires

    # Load persisted token on first call.
    if not _id_token:
        try:
            if _TOKEN_CACHE_FILE.exists():
                data = json.loads(_TOKEN_CACHE_FILE.read_text(encoding="utf-8"))
                _id_token = data.get("token", "")
                _token_expires = float(data.get("expires", 0))
        except Exception:  # noqa: BLE001
            pass

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
            # Persist so next restart skips this round-trip.
            try:
                _TOKEN_CACHE_FILE.write_text(
                    json.dumps({"token": _id_token, "expires": _token_expires}),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                pass
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


# ---------------------------------------------------------------------------
# Performance record sync
# ---------------------------------------------------------------------------


def _try_push_record(record: dict) -> bool:
    """Attempt a single Firestore PATCH for one performance record.

    Returns True on success, False on any error (network, auth, etc.).
    No-op (returns True) when Firestore is not configured so unconfigured
    installations don't accumulate outbox entries.
    """
    cfg = _config()
    if not cfg:
        return True  # not configured — treat as "no sync needed"
    project, key = cfg
    sync_key = record.get("sync_key")
    if not sync_key:
        return True  # malformed record — discard rather than loop forever
    sync_id = _get_sync_id()
    url = (
        f"{_perf_base_url(project, sync_id)}/{sync_key}"
        f"?key={urllib.parse.quote(key, safe='')}"
    )
    result = _http(url, method="PATCH", body=_to_firestore(record))
    if result:
        _log.debug("Pushed performance record %s.", sync_key)
        return True
    _log.debug("Failed to push performance record %s — queued in outbox.", sync_key)
    return False


def _append_to_outbox(record: dict) -> None:
    """Save a failed-push record to the local outbox file for later retry."""
    with _outbox_lock:
        pending: list[dict] = []
        if _OUTBOX_PATH.exists():
            try:
                pending = json.loads(_OUTBOX_PATH.read_text(encoding="utf-8"))
                if not isinstance(pending, list):
                    pending = []
            except Exception:  # noqa: BLE001
                pending = []
        pending.append(record)
        try:
            _OUTBOX_PATH.write_text(
                json.dumps(pending, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("Could not write outbox: %s", exc)


def flush_outbox() -> None:
    """Push any previously queued records to Firestore.

    Called at the start of every sync session.  Records that succeed are
    removed; records that still fail stay in the outbox for the next attempt.
    The outbox file is deleted when it becomes empty.
    """
    with _outbox_lock:
        if not _OUTBOX_PATH.exists():
            return
        try:
            pending = json.loads(_OUTBOX_PATH.read_text(encoding="utf-8"))
            if not isinstance(pending, list):
                pending = []
        except Exception:  # noqa: BLE001
            pending = []

    if not pending:
        return

    still_pending: list[dict] = []
    for record in pending:
        if not _try_push_record(record):
            still_pending.append(record)

    with _outbox_lock:
        try:
            if still_pending:
                _OUTBOX_PATH.write_text(
                    json.dumps(still_pending, ensure_ascii=False), encoding="utf-8"
                )
                _log.info(
                    "Outbox flush: %d pushed, %d still pending.",
                    len(pending) - len(still_pending),
                    len(still_pending),
                )
            else:
                _OUTBOX_PATH.unlink(missing_ok=True)
                _log.info("Outbox flush: all %d record(s) pushed.", len(pending))
        except Exception as exc:  # noqa: BLE001
            _log.warning("Could not update outbox after flush: %s", exc)


def push_performance_record(record: dict) -> None:
    """Write one answer record to Firestore, queuing it offline if unavailable.

    ``record`` must include a ``sync_key`` field (uuid4 hex) which becomes
    the Firestore document ID so the same record is never written twice.
    On network failure the record is saved to the local outbox and retried
    automatically the next time ``maybe_sync_performance`` runs.
    """
    if not _try_push_record(record):
        _append_to_outbox(record)


def _run_records_query(
    project: str, key: str, since_ts: int, *, all_devices: bool
) -> list[dict]:
    """Run a Firestore structured query against performance record documents.

    When ``all_devices`` is True a *collection group* query is issued at the
    database root so records from every device's sync_id are returned.  When
    False, only the current device's own ``records`` subcollection is queried
    (kept for the public ``pull_performance_records`` helper).
    """
    if all_devices:
        # Collection group query — parent is the database root, allDescendants
        # tells Firestore to search every "records" collection at any depth.
        parent = (
            f"https://firestore.googleapis.com/v1/projects/{project}"
            "/databases/(default)/documents"
        )
        from_clause: dict = {"collectionId": "records", "allDescendants": True}
    else:
        sync_id = _get_sync_id()
        parent = (
            f"https://firestore.googleapis.com/v1/projects/{project}"
            f"/databases/(default)/documents/{_PERF_COLLECTION}/{sync_id}"
        )
        from_clause = {"collectionId": "records"}

    url = f"{parent}:runQuery?key={urllib.parse.quote(key, safe='')}"
    structured_query: dict = {"from": [from_clause]}
    if since_ts > 0:
        structured_query["where"] = {
            "fieldFilter": {
                "field": {"fieldPath": "answered_at"},
                "op": "GREATER_THAN",
                "value": {"integerValue": str(since_ts)},
            }
        }

    result = _http(url, method="POST", body={"structuredQuery": structured_query})
    if not result:
        return []

    items: list[Any] = result if isinstance(result, list) else []
    records: list[dict] = []
    for item in items:
        doc = item.get("document") if isinstance(item, dict) else None
        if not doc:
            continue
        try:
            r = _from_firestore(doc)
            if r:
                records.append(r)
        except Exception:  # noqa: BLE001
            pass
    return records


def _pull_performance_since(
    project: str, key: str, sync_id: str, since_ts: int  # noqa: ARG001
) -> list[dict]:
    """Fetch records newer than since_ts from ALL devices via collection group query.

    The ``sync_id`` parameter is kept for backward compatibility but is no
    longer used — the collection group query searches every device's
    ``records`` subcollection so Android answers reach the desktop automatically.
    """
    return _run_records_query(project, key, since_ts, all_devices=True)


def pull_performance_records() -> list[dict]:
    """Fetch all performance records for this sync identity from Firestore."""
    cfg = _config()
    if not cfg:
        return []
    project, key = cfg
    return _run_records_query(project, key, since_ts=0, all_devices=False)


def maybe_sync_performance(col: Any) -> None:
    """Pull only new remote performance records and merge them into the local DB.

    Incremental: queries Firestore for records with answered_at greater than
    the local maximum so only genuinely new records are downloaded.
    Batch-inserts all new rows in a single executemany call.

    Also flushes the local outbox first so any records that failed to push
    while offline are sent before the pull begins.
    """
    # Flush outbox before pulling so records answered while offline reach
    # Firestore and can propagate to other devices.
    try:
        flush_outbox()
    except Exception as exc:  # noqa: BLE001
        _log.warning("Outbox flush failed: %s", exc)

    try:
        from aqt.speedrun.performance_score import PERFORMANCE_TABLE, ensure_table

        ensure_table(col)

        # Only fetch records newer than what we already have.
        row = col.db.first(f"select max(answered_at) from {PERFORMANCE_TABLE}")
        since_ts = int(row[0]) if row and row[0] else 0

        cfg = _config()
        if not cfg:
            return
        project, key = cfg
        sync_id = _get_sync_id()

        remote = _pull_performance_since(project, key, sync_id, since_ts)
        if not remote:
            _log.debug("Performance sync: no new records since ts=%d.", since_ts)
            return

        rows = [
            [
                int(r.get("answered_at") or 0),
                str(r.get("question_id", "")),
                str(r.get("topic", "")),
                str(r.get("concept", "")),
                str(r.get("chosen_concept", "")),
                str(r.get("correct_concept", "")),
                str(r.get("chosen_answer", "")),
                str(r.get("correct_answer", "")),
                int(r.get("concept_correct") or 0),
                int(r.get("application_correct") or 0),
                int(r.get("answer_correct") or 0),
                r.get("sync_key", ""),
            ]
            for r in remote
            if r.get("sync_key")
        ]

        if rows:
            col.db.executemany(
                f"""
                insert or ignore into {PERFORMANCE_TABLE}
                    (answered_at, question_id, topic, concept, chosen_concept,
                     correct_concept, chosen_answer, correct_answer,
                     concept_correct, application_correct, answer_correct,
                     sync_key)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            _log.info(
                "Synced %d new performance record(s) from Firestore.", len(rows)
            )
    except Exception as exc:  # noqa: BLE001
        _log.warning("maybe_sync_performance failed: %s", exc)
