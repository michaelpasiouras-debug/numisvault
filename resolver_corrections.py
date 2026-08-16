"""
CoinBids — Resolver Corrections Store.

Implements the feedback loop the original Coin Intelligence Core handoff
spec called out as the "NEXT REQUIRED PRODUCTION STEP":

    Add a persistent resolver-corrections store so an admin/user can say:
    "this alias means X" or "this result was wrong"
    Store corrections separately from code, with:
    - raw_input
    - corrected canonical identity
    - timestamp
    - reason
    - optional listing URL/source
    Do NOT silently self-train from arbitrary user clicks.

DESIGN NOTES
- Every correction requires an explicit `reason` string. There is no
  endpoint or code path that adds a correction automatically from ordinary
  usage (e.g. clicking a search result) — a correction only exists because
  someone deliberately submitted one via /api/resolver/corrections, which
  itself requires a shared secret (see below). This satisfies "do NOT
  silently self-train from arbitrary user clicks".
- Storage is a plain JSON file, one record per correction, guarded by a
  thread lock and written atomically (write-to-temp-then-rename) so a crash
  mid-write can't corrupt the file.
- HONESTY ABOUT DURABILITY: on Render's free tier (and most free/ephemeral
  hosting), the local filesystem is NOT guaranteed to survive a redeploy,
  restart, or dyno/instance replacement. This module does not pretend
  otherwise. For durable storage across deploys, mount a persistent disk
  (Render "Disks" add-on) or point COINBIDS_CORRECTIONS_PATH at a path on
  one, or migrate to an external store (small managed Postgres/Redis) later
  — the CorrectionsStore interface below does not need to change for that,
  only its internal read/write implementation would.
- Corrections are looked up by the SAME normalization the resolver itself
  uses (coin_identity_resolver.norm), so a correction for "5 drahcma 1976"
  also matches "5 DRAHCMA 1976" or "5   drahcma   1976" — but does NOT fuzzy-
  match anything else. An exact-normalized-text override only, on purpose:
  broader fuzzy application of a manual correction is exactly the kind of
  silent self-training the spec explicitly prohibits.
"""
from __future__ import annotations
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from typing import List, Optional

try:
    from coin_identity_resolver import norm
except Exception:
    def norm(s):  # pragma: no cover - fallback if resolver unavailable
        return (s or "").strip().lower()

DEFAULT_PATH = os.environ.get("COINBIDS_CORRECTIONS_PATH",
                               os.path.join(os.path.dirname(__file__), "resolver_corrections.json"))

_LOCK = threading.Lock()


class CorrectionsStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path

    def _read_all(self) -> List[dict]:
        if not os.path.exists(self.path):
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            # A corrupted or unreadable file must never crash the app — treat
            # it as empty rather than raising past this boundary. The
            # original file is left on disk for manual inspection/recovery.
            return []

    def _write_all(self, records: List[dict]) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".corrections_tmp_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)  # atomic on POSIX
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise

    def add(self, raw_input: str, corrected: dict, reason: str,
             source_url: Optional[str] = None, submitted_by: Optional[str] = None) -> dict:
        if not raw_input or not raw_input.strip():
            raise ValueError("raw_input is required")
        if not corrected or not isinstance(corrected, dict):
            raise ValueError("corrected must be a non-empty identity dict")
        if not reason or not reason.strip():
            raise ValueError("reason is required — corrections must never be silent/unexplained")
        with _LOCK:
            records = self._read_all()
            record = {
                "id": uuid.uuid4().hex[:12],
                "raw_input": raw_input,
                "normalized_key": norm(raw_input),
                "corrected": corrected,
                "reason": reason.strip(),
                "source_url": source_url,
                "submitted_by": submitted_by,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # A newer correction for the same normalized input supersedes an
            # older one rather than accumulating duplicates silently.
            records = [r for r in records if r.get("normalized_key") != record["normalized_key"]]
            records.append(record)
            self._write_all(records)
        return record

    def get_override(self, raw_input: str) -> Optional[dict]:
        key = norm(raw_input)
        if not key:
            return None
        with _LOCK:
            records = self._read_all()
        for r in records:
            if r.get("normalized_key") == key:
                return r
        return None

    def list_all(self) -> List[dict]:
        with _LOCK:
            return self._read_all()

    def delete(self, correction_id: str) -> bool:
        with _LOCK:
            records = self._read_all()
            new_records = [r for r in records if r.get("id") != correction_id]
            if len(new_records) == len(records):
                return False
            self._write_all(new_records)
            return True


_default_store: Optional[CorrectionsStore] = None


def get_store() -> CorrectionsStore:
    global _default_store
    if _default_store is None:
        _default_store = CorrectionsStore()
    return _default_store
