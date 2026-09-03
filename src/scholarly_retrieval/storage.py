"""SQLite persistence for raw responses, cache entries, attempts, and run manifests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import Any

from .models import (
    CitationAssertion,
    CitationEdgeStatusEvent,
    CitationVerificationStatus,
    EntityRelationKind,
    IdentityEvent,
    IdentityEventAction,
    VisibleCitationEdge,
)

SCHEMA_VERSION = 4
SAFE_RESPONSE_HEADERS = {
    "cache-control",
    "content-type",
    "etag",
    "last-modified",
    "retry-after",
}


@dataclass(frozen=True)
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    retrieved_at: float
    expires_at: float


@dataclass(frozen=True)
class CursorCheckpoint:
    provider: str
    operation: str
    cursor: str | None
    state: dict[str, Any]
    updated_at: float


def make_checkpoint_key(provider: str, operation: str, identifier: str, limit: int) -> str:
    """Return a stable, non-plaintext key for one resumable logical request."""

    material = json.dumps(
        [provider, operation, identifier, limit],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(material).hexdigest()


class SQLiteStore:
    """Small synchronous store; operations are short and protected per connection."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, timeout=5.0)
        self._connection.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                # A live backup must include WAL/SHM, or run after close() has
                # checkpointed the WAL into the main database file.
                self._connection.execute("PRAGMA journal_mode = WAL")
                self._connection.execute("PRAGMA synchronous = NORMAL")

    def _migrate(self) -> None:
        script = """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS http_cache (
            cache_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            method TEXT NOT NULL,
            url TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            body BLOB NOT NULL,
            retrieved_at REAL NOT NULL,
            expires_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_http_cache_expiry ON http_cache(expires_at);
        CREATE TABLE IF NOT EXISTS raw_responses (
            response_id TEXT PRIMARY KEY,
            cache_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            method TEXT NOT NULL,
            url TEXT NOT NULL,
            status_code INTEGER NOT NULL,
            headers_json TEXT NOT NULL,
            body_sha256 TEXT NOT NULL,
            body BLOB NOT NULL,
            retrieved_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_raw_response_key ON raw_responses(cache_key, retrieved_at);
        CREATE TABLE IF NOT EXISTS provider_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            attempt_number INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            status_code INTEGER,
            error_type TEXT,
            retry_delay REAL,
            occurred_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_attempt_provider ON provider_attempts(provider, occurred_at);
        CREATE TABLE IF NOT EXISTS query_runs (
            run_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            request_json TEXT NOT NULL,
            status TEXT NOT NULL,
            result_json TEXT,
            result_fingerprint TEXT,
            started_at REAL NOT NULL,
            finished_at REAL
        );
        CREATE TABLE IF NOT EXISTS cursor_checkpoints (
            checkpoint_key TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            operation TEXT NOT NULL,
            cursor TEXT,
            state_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS identity_events (
            event_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            relation TEXT NOT NULL,
            left_record_id TEXT NOT NULL,
            right_record_id TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            reverts_event_id TEXT REFERENCES identity_events(event_id),
            reverted_by_event_id TEXT REFERENCES identity_events(event_id)
        );
        CREATE INDEX IF NOT EXISTS idx_identity_event_pair
            ON identity_events(left_record_id, right_record_id, created_at);
        CREATE TABLE IF NOT EXISTS citation_assertions (
            assertion_id TEXT PRIMARY KEY,
            subject_record_id TEXT NOT NULL,
            object_record_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            provider TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            evidence_type TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_citation_assertion_endpoints
            ON citation_assertions(subject_record_id, object_record_id);
        CREATE TABLE IF NOT EXISTS visible_citation_edges (
            edge_id TEXT PRIMARY KEY,
            citing_record_id TEXT NOT NULL,
            cited_record_id TEXT NOT NULL,
            verification_status TEXT NOT NULL,
            first_seen_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_visible_citation_endpoints
            ON visible_citation_edges(citing_record_id, cited_record_id);
        CREATE TABLE IF NOT EXISTS citation_edge_assertions (
            edge_id TEXT NOT NULL REFERENCES visible_citation_edges(edge_id),
            assertion_id TEXT NOT NULL REFERENCES citation_assertions(assertion_id),
            PRIMARY KEY(edge_id, assertion_id)
        );
        CREATE TABLE IF NOT EXISTS citation_edge_status_events (
            event_id TEXT PRIMARY KEY,
            edge_id TEXT NOT NULL REFERENCES visible_citation_edges(edge_id),
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            triggering_assertion_id TEXT NOT NULL REFERENCES citation_assertions(assertion_id),
            occurred_at TEXT NOT NULL,
            UNIQUE(edge_id, previous_status, new_status)
        );
        CREATE INDEX IF NOT EXISTS idx_citation_edge_event_edge
            ON citation_edge_status_events(edge_id, occurred_at);
        """
        with self._lock:
            self._connection.executescript(script)
            # CREATE TABLE IF NOT EXISTS does not evolve an existing v1 table.
            # Keep migrations additive so a user's accumulated audit data remains usable.
            query_run_columns = {
                row["name"]
                for row in self._connection.execute("PRAGMA table_info(query_runs)").fetchall()
            }
            if "result_fingerprint" not in query_run_columns:
                self._connection.execute(
                    "ALTER TABLE query_runs ADD COLUMN result_fingerprint TEXT"
                )
            self._connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._connection.commit()

    def get_cached_response(
        self, cache_key: str, *, now: float | None = None
    ) -> CachedResponse | None:
        timestamp = time() if now is None else now
        with self._lock:
            row = self._connection.execute(
                "SELECT status_code, headers_json, body, retrieved_at, expires_at "
                "FROM http_cache WHERE cache_key = ? AND expires_at > ?",
                (cache_key, timestamp),
            ).fetchone()
        if row is None:
            return None
        return CachedResponse(
            status_code=row["status_code"],
            headers=json.loads(row["headers_json"]),
            body=bytes(row["body"]),
            retrieved_at=row["retrieved_at"],
            expires_at=row["expires_at"],
        )

    def record_response(
        self,
        *,
        cache_key: str,
        provider: str,
        method: str,
        url: str,
        status_code: int,
        headers: dict[str, str],
        body: bytes,
        cache_ttl_seconds: float | None,
        retrieved_at: float | None = None,
    ) -> str:
        timestamp = time() if retrieved_at is None else retrieved_at
        safe_headers = {
            key.lower(): value
            for key, value in headers.items()
            if key.lower() in SAFE_RESPONSE_HEADERS
        }
        headers_json = json.dumps(safe_headers, sort_keys=True, ensure_ascii=False)
        response_id = str(uuid.uuid4())
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            self._connection.execute(
                "INSERT INTO raw_responses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    response_id,
                    cache_key,
                    provider,
                    method,
                    url,
                    status_code,
                    headers_json,
                    digest,
                    body,
                    timestamp,
                ),
            )
            if cache_ttl_seconds is not None and cache_ttl_seconds > 0 and status_code == 200:
                self._connection.execute(
                    "INSERT OR REPLACE INTO http_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cache_key,
                        provider,
                        method,
                        url,
                        status_code,
                        headers_json,
                        body,
                        timestamp,
                        timestamp + cache_ttl_seconds,
                    ),
                )
            self._connection.commit()
        return response_id

    def record_attempt(
        self,
        *,
        provider: str,
        operation: str,
        cache_key: str,
        attempt_number: int,
        outcome: str,
        status_code: int | None = None,
        error_type: str | None = None,
        retry_delay: float | None = None,
        run_id: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO provider_attempts(
                    run_id, provider, operation, cache_key, attempt_number, outcome,
                    status_code, error_type, retry_delay, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    provider,
                    operation,
                    cache_key,
                    attempt_number,
                    outcome,
                    status_code,
                    error_type,
                    retry_delay,
                    time(),
                ),
            )
            self._connection.commit()

    def begin_run(self, operation: str, request: dict[str, Any]) -> str:
        run_id = str(uuid.uuid4())
        with self._lock:
            self._connection.execute(
                """INSERT INTO query_runs(
                    run_id, operation, request_json, status, result_json,
                    result_fingerprint, started_at, finished_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)""",
                (
                    run_id,
                    operation,
                    json.dumps(request, sort_keys=True, ensure_ascii=False),
                    "running",
                    time(),
                ),
            )
            self._connection.commit()
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        fingerprint: str | None = None,
    ) -> None:
        result_json = (
            json.dumps(result, sort_keys=True, ensure_ascii=False) if result is not None else None
        )
        with self._lock:
            self._connection.execute(
                "UPDATE query_runs SET status = ?, result_json = ?, result_fingerprint = ?, "
                "finished_at = ? WHERE run_id = ?",
                (status, result_json, fingerprint, time(), run_id),
            )
            self._connection.commit()

    def save_checkpoint(
        self,
        checkpoint_key: str,
        *,
        provider: str,
        operation: str,
        cursor: str | None,
        state: dict[str, Any],
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO cursor_checkpoints VALUES (?, ?, ?, ?, ?, ?)",
                (
                    checkpoint_key,
                    provider,
                    operation,
                    cursor,
                    json.dumps(state, sort_keys=True, ensure_ascii=False),
                    time(),
                ),
            )
            self._connection.commit()

    def load_checkpoint(self, checkpoint_key: str) -> CursorCheckpoint | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT provider, operation, cursor, state_json, updated_at "
                "FROM cursor_checkpoints WHERE checkpoint_key = ?",
                (checkpoint_key,),
            ).fetchone()
        if row is None:
            return None
        state = json.loads(row["state_json"])
        if not isinstance(state, dict):
            raise ValueError("checkpoint state must be a JSON object")
        return CursorCheckpoint(
            provider=row["provider"],
            operation=row["operation"],
            cursor=row["cursor"],
            state=state,
            updated_at=row["updated_at"],
        )

    def delete_checkpoint(self, checkpoint_key: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM cursor_checkpoints WHERE checkpoint_key = ?",
                (checkpoint_key,),
            )
            self._connection.commit()

    def record_identity_event(
        self,
        *,
        action: IdentityEventAction,
        relation: EntityRelationKind,
        left_record_id: str,
        right_record_id: str,
        reasons: list[str],
    ) -> IdentityEvent:
        """Append a reviewed identity decision without mutating canonical papers."""

        left = left_record_id.strip()
        right = right_record_id.strip()
        normalized_reasons = [reason.strip() for reason in reasons if reason.strip()]
        if not left or not right:
            raise ValueError("identity event record IDs must not be blank")
        if left == right:
            raise ValueError("identity event record IDs must be different")
        if not normalized_reasons:
            raise ValueError("identity event requires at least one reason")
        if action == IdentityEventAction.REVERT:
            raise ValueError("use revert_identity_event to create rollback events")
        event = IdentityEvent(
            event_id=str(uuid.uuid4()),
            action=action,
            relation=relation,
            left_record_id=left,
            right_record_id=right,
            reasons=normalized_reasons,
        )
        with self._lock:
            self._insert_identity_event(event)
            self._connection.commit()
        return event

    def revert_identity_event(self, event_id: str, *, reason: str) -> IdentityEvent:
        """Append a rollback event and link it to the retained original event."""

        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("rollback reason must not be blank")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM identity_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"identity event not found: {event_id}")
            original = self._identity_event_from_row(row)
            if original.action == IdentityEventAction.REVERT:
                raise ValueError("a rollback event cannot itself be reverted")
            if original.reverted_by_event_id is not None:
                raise ValueError("identity event has already been reverted")
            rollback = IdentityEvent(
                event_id=str(uuid.uuid4()),
                action=IdentityEventAction.REVERT,
                relation=original.relation,
                left_record_id=original.left_record_id,
                right_record_id=original.right_record_id,
                reasons=[normalized_reason],
                reverts_event_id=original.event_id,
            )
            # One transaction makes the append and the reverse link atomic.
            self._insert_identity_event(rollback)
            self._connection.execute(
                "UPDATE identity_events SET reverted_by_event_id = ? WHERE event_id = ?",
                (rollback.event_id, original.event_id),
            )
            self._connection.commit()
        return rollback

    def list_identity_events(self, *, active_only: bool = False) -> list[IdentityEvent]:
        predicate = (
            "WHERE reverted_by_event_id IS NULL AND action != 'revert'" if active_only else ""
        )
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM identity_events {predicate} ORDER BY created_at, event_id"
            ).fetchall()
        return [self._identity_event_from_row(row) for row in rows]

    def _insert_identity_event(self, event: IdentityEvent) -> None:
        self._connection.execute(
            """INSERT INTO identity_events(
                event_id, action, relation, left_record_id, right_record_id,
                reasons_json, created_at, reverts_event_id, reverted_by_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.action.value,
                event.relation.value,
                event.left_record_id,
                event.right_record_id,
                json.dumps(event.reasons, ensure_ascii=False),
                event.created_at.isoformat(),
                event.reverts_event_id,
                event.reverted_by_event_id,
            ),
        )

    @staticmethod
    def _identity_event_from_row(row: sqlite3.Row) -> IdentityEvent:
        created_at = datetime.fromisoformat(row["created_at"])
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return IdentityEvent(
            event_id=row["event_id"],
            action=row["action"],
            relation=row["relation"],
            left_record_id=row["left_record_id"],
            right_record_id=row["right_record_id"],
            reasons=json.loads(row["reasons_json"]),
            created_at=created_at,
            reverts_event_id=row["reverts_event_id"],
            reverted_by_event_id=row["reverted_by_event_id"],
        )

    def record_citation_evidence(
        self,
        assertions: list[CitationAssertion],
        edges: list[VisibleCitationEdge],
    ) -> None:
        """Persist assertions and visible edges without losing weaker evidence.

        Assertions are immutable evidence identities and are upserted idempotently.
        Edge verification is monotonic: later provider-only observations cannot
        downgrade an edge once full text has verified it.
        """

        assertions_by_id = {
            assertion.assertion_id: assertion
            for assertion in [*assertions, *(item for edge in edges for item in edge.assertions)]
            if assertion.assertion_id is not None
        }
        timestamp = time()
        with self._lock:
            try:
                for assertion in assertions_by_id.values():
                    self._upsert_citation_assertion(assertion, timestamp)
                for edge in edges:
                    self._upsert_visible_citation_edge(edge, timestamp)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _upsert_citation_assertion(
        self,
        assertion: CitationAssertion,
        timestamp: float,
    ) -> None:
        if assertion.assertion_id is None:
            raise ValueError("citation assertion requires a stable assertion_id")
        self._connection.execute(
            """INSERT INTO citation_assertions(
                assertion_id, subject_record_id, object_record_id, relation,
                provider, source_record_id, evidence_type, verification_status,
                provenance_json, evidence_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(assertion_id) DO UPDATE SET
                verification_status = CASE
                    WHEN citation_assertions.verification_status = 'verified'
                    THEN citation_assertions.verification_status
                    ELSE excluded.verification_status
                END,
                provenance_json = excluded.provenance_json,
                evidence_json = excluded.evidence_json,
                last_seen_at = excluded.last_seen_at""",
            (
                assertion.assertion_id,
                assertion.subject_record_id,
                assertion.object_record_id,
                assertion.relation.value,
                assertion.provenance.provider,
                assertion.provenance.source_record_id,
                assertion.evidence_type.value,
                assertion.verification_status.value,
                json.dumps(
                    assertion.provenance.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                json.dumps(assertion.evidence, sort_keys=True, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )

    def _upsert_visible_citation_edge(
        self,
        edge: VisibleCitationEdge,
        timestamp: float,
    ) -> None:
        if edge.edge_id is None:
            raise ValueError("visible citation edge requires a stable edge_id")
        existing = self._connection.execute(
            "SELECT verification_status FROM visible_citation_edges WHERE edge_id = ?",
            (edge.edge_id,),
        ).fetchone()
        previous_status = (
            CitationVerificationStatus(existing["verification_status"])
            if existing is not None
            else None
        )
        target_status = (
            CitationVerificationStatus.VERIFIED
            if edge.verification_status == CitationVerificationStatus.VERIFIED
            or previous_status == CitationVerificationStatus.VERIFIED
            else CitationVerificationStatus.PROVIDER_ASSERTED
        )
        self._connection.execute(
            """INSERT INTO visible_citation_edges(
                edge_id, citing_record_id, cited_record_id, verification_status,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(edge_id) DO UPDATE SET
                verification_status = excluded.verification_status,
                last_seen_at = excluded.last_seen_at""",
            (
                edge.edge_id,
                edge.citing_record_id,
                edge.cited_record_id,
                target_status.value,
                timestamp,
                timestamp,
            ),
        )
        assertion_ids = [
            assertion.assertion_id
            for assertion in edge.assertions
            if assertion.assertion_id is not None
        ]
        self._connection.executemany(
            "INSERT OR IGNORE INTO citation_edge_assertions(edge_id, assertion_id) VALUES (?, ?)",
            ((edge.edge_id, assertion_id) for assertion_id in assertion_ids),
        )
        if (
            previous_status == CitationVerificationStatus.PROVIDER_ASSERTED
            and target_status == CitationVerificationStatus.VERIFIED
        ):
            triggering_id = next(
                (
                    assertion.assertion_id
                    for assertion in edge.assertions
                    if assertion.verification_status == CitationVerificationStatus.VERIFIED
                    and assertion.assertion_id is not None
                ),
                None,
            )
            if triggering_id is None:
                raise ValueError("verified citation edge requires a verified assertion")
            event = CitationEdgeStatusEvent(
                event_id=f"ceu:{uuid.uuid4()}",
                edge_id=edge.edge_id,
                previous_status=previous_status,
                new_status=target_status,
                triggering_assertion_id=triggering_id,
            )
            self._connection.execute(
                """INSERT OR IGNORE INTO citation_edge_status_events(
                    event_id, edge_id, previous_status, new_status,
                    triggering_assertion_id, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    event.edge_id,
                    event.previous_status.value,
                    event.new_status.value,
                    event.triggering_assertion_id,
                    event.occurred_at.isoformat(),
                ),
            )

    def get_citation_edge(self, edge_id: str) -> VisibleCitationEdge | None:
        """Reconstruct one visible edge and every retained source assertion."""

        with self._lock:
            edge_row = self._connection.execute(
                "SELECT * FROM visible_citation_edges WHERE edge_id = ?",
                (edge_id,),
            ).fetchone()
            if edge_row is None:
                return None
            assertion_rows = self._connection.execute(
                """SELECT assertion.* FROM citation_assertions AS assertion
                JOIN citation_edge_assertions AS link
                  ON link.assertion_id = assertion.assertion_id
                WHERE link.edge_id = ?
                ORDER BY assertion.first_seen_at, assertion.assertion_id""",
                (edge_id,),
            ).fetchall()
        return VisibleCitationEdge(
            edge_id=edge_row["edge_id"],
            citing_record_id=edge_row["citing_record_id"],
            cited_record_id=edge_row["cited_record_id"],
            assertions=[self._citation_assertion_from_row(row) for row in assertion_rows],
            verification_status=edge_row["verification_status"],
        )

    def list_citation_edge_events(
        self,
        *,
        edge_id: str | None = None,
    ) -> list[CitationEdgeStatusEvent]:
        predicate = "WHERE edge_id = ?" if edge_id is not None else ""
        parameters = (edge_id,) if edge_id is not None else ()
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM citation_edge_status_events {predicate} "
                "ORDER BY occurred_at, event_id",
                parameters,
            ).fetchall()
        return [
            CitationEdgeStatusEvent(
                event_id=row["event_id"],
                edge_id=row["edge_id"],
                previous_status=row["previous_status"],
                new_status=row["new_status"],
                triggering_assertion_id=row["triggering_assertion_id"],
                occurred_at=datetime.fromisoformat(row["occurred_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _citation_assertion_from_row(row: sqlite3.Row) -> CitationAssertion:
        return CitationAssertion(
            assertion_id=row["assertion_id"],
            subject_record_id=row["subject_record_id"],
            object_record_id=row["object_record_id"],
            relation=row["relation"],
            provenance=json.loads(row["provenance_json"]),
            evidence_type=row["evidence_type"],
            verification_status=row["verification_status"],
            evidence=json.loads(row["evidence_json"]),
        )

    def stats(self) -> dict[str, int]:
        tables = (
            "http_cache",
            "raw_responses",
            "provider_attempts",
            "query_runs",
            "cursor_checkpoints",
            "identity_events",
            "citation_assertions",
            "visible_citation_edges",
            "citation_edge_status_events",
        )
        with self._lock:
            return {
                table: self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

    def prune(
        self,
        *,
        retention_days: int = 30,
        max_raw_responses: int | None = None,
        apply: bool = False,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Preview or delete expired cache and old audit/recovery records."""

        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if max_raw_responses is not None and max_raw_responses < 0:
            raise ValueError("max_raw_responses must not be negative")
        timestamp = time() if now is None else now
        cutoff = timestamp - retention_days * 86400
        predicates = {
            "http_cache": ("expires_at <= ?", timestamp),
            "raw_responses": ("retrieved_at < ?", cutoff),
            "provider_attempts": ("occurred_at < ?", cutoff),
            "query_runs": ("finished_at IS NOT NULL AND finished_at < ?", cutoff),
            "cursor_checkpoints": ("updated_at < ?", cutoff),
        }
        with self._lock:
            counts = {
                table: self._connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {predicate}",
                    (threshold,),
                ).fetchone()[0]
                for table, (predicate, threshold) in predicates.items()
            }
            capacity_ids: list[str] = []
            if max_raw_responses is not None:
                total_raw = self._connection.execute(
                    "SELECT COUNT(*) FROM raw_responses"
                ).fetchone()[0]
                excess = max(0, total_raw - max_raw_responses)
                capacity_ids = [
                    row[0]
                    for row in self._connection.execute(
                        "SELECT response_id FROM raw_responses "
                        "ORDER BY retrieved_at, response_id LIMIT ?",
                        (excess,),
                    ).fetchall()
                ]
                old_ids = {
                    row[0]
                    for row in self._connection.execute(
                        "SELECT response_id FROM raw_responses WHERE retrieved_at < ?",
                        (cutoff,),
                    ).fetchall()
                }
                counts["raw_responses"] = len(old_ids | set(capacity_ids))
            if apply:
                for table, (predicate, threshold) in predicates.items():
                    self._connection.execute(f"DELETE FROM {table} WHERE {predicate}", (threshold,))
                if capacity_ids:
                    self._connection.executemany(
                        "DELETE FROM raw_responses WHERE response_id = ?",
                        ((response_id,) for response_id in capacity_ids),
                    )
                self._connection.commit()
        return {
            "applied": apply,
            "retention_days": retention_days,
            "max_raw_responses": max_raw_responses,
            "eligible": counts,
            "deleted": counts if apply else {table: 0 for table in counts},
        }

    def close(self) -> None:
        with self._lock:
            if self.path != ":memory:":
                self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.close()
