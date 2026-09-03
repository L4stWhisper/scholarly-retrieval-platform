import sqlite3
from pathlib import Path
from uuid import uuid4

from scholarly_retrieval.models import (
    CitationAssertion,
    CitationEvidenceType,
    CitationVerificationStatus,
    EntityRelationKind,
    IdentityEventAction,
    Provenance,
    RelationKind,
    VisibleCitationEdge,
)
from scholarly_retrieval.storage import SCHEMA_VERSION, SQLiteStore


def test_query_run_checkpoint_and_reopen() -> None:
    database = Path.cwd() / f".test-storage-{uuid4()}.sqlite3"
    try:
        store = SQLiteStore(database)
        run_id = store.begin_run("search", {"text": "graph"})
        store.finish_run(
            run_id,
            status="complete",
            result={"papers": 1},
            fingerprint="v1:fixture",
        )
        store.save_checkpoint(
            "openalex:search:graph",
            provider="openalex",
            operation="search",
            cursor="next",
            state={"retrieved": 100},
        )
        assert store.stats()["query_runs"] == 1
        loaded = store.load_checkpoint("openalex:search:graph")
        assert loaded is not None
        assert loaded.cursor == "next"
        assert loaded.state == {"retrieved": 100}
        store.close()

        reopened = SQLiteStore(database)
        row = reopened._connection.execute(
            "SELECT status, result_json, result_fingerprint FROM query_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row["status"] == "complete"
        assert '"papers": 1' in row["result_json"]
        assert row["result_fingerprint"] == "v1:fixture"
        checkpoint = reopened._connection.execute(
            "SELECT cursor, state_json FROM cursor_checkpoints"
        ).fetchone()
        assert checkpoint["cursor"] == "next"
        assert '"retrieved": 100' in checkpoint["state_json"]
        reopened.delete_checkpoint("openalex:search:graph")
        assert reopened.load_checkpoint("openalex:search:graph") is None
        reopened.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)


def test_v1_database_is_migrated_without_losing_query_runs() -> None:
    database = Path.cwd() / f".test-storage-v1-{uuid4()}.sqlite3"
    try:
        connection = sqlite3.connect(database)
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            CREATE TABLE query_runs (
                run_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                request_json TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                started_at REAL NOT NULL,
                finished_at REAL
            );
            INSERT INTO query_runs VALUES (
                'legacy-run', 'search', '{}', 'complete', '{}', 1.0, 2.0
            );
            """
        )
        connection.commit()
        connection.close()

        store = SQLiteStore(database)
        columns = {
            row["name"]
            for row in store._connection.execute("PRAGMA table_info(query_runs)").fetchall()
        }
        version = store._connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        assert "result_fingerprint" in columns
        assert version == str(SCHEMA_VERSION)
        assert {
            "citation_assertions",
            "visible_citation_edges",
            "citation_edge_assertions",
            "citation_edge_status_events",
        }.issubset(
            {
                row[0]
                for row in store._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        )
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM query_runs WHERE run_id = 'legacy-run'"
            ).fetchone()[0]
            == 1
        )

        # Explicit insert columns make new writes valid even though ALTER TABLE
        # appends the migrated column in a different physical order.
        new_run = store.begin_run("resolve", {"identifier": "10.1/example"})
        store.finish_run(new_run, status="complete", result={}, fingerprint="v1:new")
        store.close()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)


def test_storage_prune_previews_before_deleting_expired_records() -> None:
    store = SQLiteStore(":memory:")
    store.record_response(
        cache_key="old",
        provider="fixture",
        method="GET",
        url="https://example.org/data",
        status_code=200,
        headers={},
        body=b"{}",
        cache_ttl_seconds=1,
        retrieved_at=0,
    )

    preview = store.prune(retention_days=1, now=200_000)
    assert preview["applied"] is False
    assert preview["eligible"]["http_cache"] == 1
    assert preview["eligible"]["raw_responses"] == 1
    assert store.stats()["http_cache"] == 1

    applied = store.prune(retention_days=1, now=200_000, apply=True)
    assert applied["deleted"]["http_cache"] == 1
    assert applied["deleted"]["raw_responses"] == 1
    assert store.stats()["http_cache"] == 0
    assert store.stats()["raw_responses"] == 0
    store.close()


def test_identity_decisions_are_auditable_and_reversible() -> None:
    store = SQLiteStore(":memory:")
    original = store.record_identity_event(
        action=IdentityEventAction.MERGE,
        relation=EntityRelationKind.SAME_MANIFESTATION,
        left_record_id="openalex:W1",
        right_record_id="s2:S1",
        reasons=["curator verified the publisher DOI"],
    )

    rollback = store.revert_identity_event(
        original.event_id, reason="publisher issued a correction"
    )
    events = store.list_identity_events()
    assert [event.action for event in events] == [
        IdentityEventAction.MERGE,
        IdentityEventAction.REVERT,
    ]
    assert events[0].reverted_by_event_id == rollback.event_id
    assert rollback.reverts_event_id == original.event_id
    assert store.list_identity_events(active_only=True) == []
    assert store.stats()["identity_events"] == 2

    try:
        store.revert_identity_event(original.event_id, reason="duplicate")
    except ValueError as exc:
        assert "already been reverted" in str(exc)
    else:
        raise AssertionError("a second rollback must be rejected")
    store.close()


def test_storage_capacity_preview_and_apply_remove_oldest_raw_responses() -> None:
    store = SQLiteStore(":memory:")
    for index in range(3):
        store.record_response(
            cache_key=f"key-{index}",
            provider="fixture",
            method="GET",
            url=f"https://example.org/{index}",
            status_code=200,
            headers={},
            body=b"{}",
            cache_ttl_seconds=None,
            retrieved_at=100 + index,
        )

    preview = store.prune(
        retention_days=365,
        max_raw_responses=2,
        now=200,
    )
    assert preview["eligible"]["raw_responses"] == 1
    assert preview["max_raw_responses"] == 2
    assert store.stats()["raw_responses"] == 3

    store.prune(
        retention_days=365,
        max_raw_responses=2,
        apply=True,
        now=200,
    )
    assert store.stats()["raw_responses"] == 2
    remaining = {
        row[0]
        for row in store._connection.execute("SELECT cache_key FROM raw_responses").fetchall()
    }
    assert remaining == {"key-1", "key-2"}
    store.close()


def test_citation_evidence_is_idempotent_and_fulltext_upgrades_visible_edge() -> None:
    store = SQLiteStore(":memory:")
    provider_assertion = CitationAssertion(
        subject_record_id="work:citing",
        object_record_id="work:cited",
        relation=RelationKind.REFERENCES,
        provenance=Provenance(provider="openalex", source_record_id="W1"),
        evidence_type=CitationEvidenceType.PROVIDER_GRAPH,
        verification_status=CitationVerificationStatus.PROVIDER_ASSERTED,
    )
    provider_edge = VisibleCitationEdge(
        citing_record_id="work:citing",
        cited_record_id="work:cited",
        assertions=[provider_assertion],
    )
    fulltext_assertion = CitationAssertion(
        subject_record_id="work:citing",
        object_record_id="work:cited",
        relation=RelationKind.REFERENCES,
        provenance=Provenance(provider="fulltext:jats", source_record_id="R1"),
        evidence_type=CitationEvidenceType.FULLTEXT_ANCHOR,
        verification_status=CitationVerificationStatus.VERIFIED,
        evidence={"citation_contexts": [{"text": "Prior work [1]."}]},
    )
    verified_edge = VisibleCitationEdge(
        citing_record_id="work:citing",
        cited_record_id="work:cited",
        assertions=[fulltext_assertion],
    )

    store.record_citation_evidence([provider_assertion], [provider_edge])
    store.record_citation_evidence([fulltext_assertion], [verified_edge])
    # Replays and a later weaker provider observation must not duplicate or downgrade.
    store.record_citation_evidence([fulltext_assertion], [verified_edge])
    store.record_citation_evidence([provider_assertion], [provider_edge])

    loaded = store.get_citation_edge(provider_edge.edge_id or "")
    assert loaded is not None
    assert loaded.verification_status == CitationVerificationStatus.VERIFIED
    assert {item.assertion_id for item in loaded.assertions} == {
        provider_assertion.assertion_id,
        fulltext_assertion.assertion_id,
    }
    events = store.list_citation_edge_events(edge_id=loaded.edge_id)
    assert len(events) == 1
    assert events[0].previous_status == CitationVerificationStatus.PROVIDER_ASSERTED
    assert events[0].new_status == CitationVerificationStatus.VERIFIED
    assert events[0].triggering_assertion_id == fulltext_assertion.assertion_id
    assert store.stats()["citation_assertions"] == 2
    assert store.stats()["visible_citation_edges"] == 1
    assert store.stats()["citation_edge_status_events"] == 1
    store.close()
