from scholarly_retrieval.identity import merge_exact_records, resolve_identities
from scholarly_retrieval.models import (
    Author,
    EntityRelationKind,
    IdentifierClaim,
    IdentifierScheme,
    IdentityDecisionKind,
    IdentityEvent,
    IdentityEventAction,
    Paper,
    Provenance,
    SourceRecord,
)


def paper(
    provider: str,
    record_id: str,
    doi: str | None,
    title: str,
    *,
    arxiv: str | None = None,
    pmid: str | None = None,
    year: int | None = None,
) -> Paper:
    provenance = Provenance(provider=provider, source_record_id=record_id)
    identifiers = []
    if doi:
        identifiers.append(
            IdentifierClaim(scheme=IdentifierScheme.DOI, value=doi, provenance=provenance)
        )
    if arxiv:
        identifiers.append(
            IdentifierClaim(
                scheme=IdentifierScheme.ARXIV,
                value=arxiv,
                provenance=provenance,
            )
        )
    if pmid:
        identifiers.append(
            IdentifierClaim(
                scheme=IdentifierScheme.PMID,
                value=pmid,
                provenance=provenance,
            )
        )
    return Paper(
        record_id=f"{provider}:{record_id}",
        title=title,
        authors=[Author(name="Ada Researcher")],
        publication_year=year,
        identifiers=identifiers,
        source_records=[SourceRecord(provider=provider, source_record_id=record_id)],
    )


def test_merge_same_doi_preserves_both_sources() -> None:
    merged = merge_exact_records(
        [
            paper("openalex", "W1", "10.1234/x", "Title"),
            paper("s2", "S1", "10.1234/x", "Title from S2"),
        ]
    )
    assert len(merged) == 1
    assert {item.provider for item in merged[0].source_records} == {"openalex", "s2"}


def test_merge_preserves_conflicting_source_field_values() -> None:
    left_provenance = Provenance(provider="openalex", source_record_id="W1")
    right_provenance = Provenance(provider="s2", source_record_id="S1")
    left = paper("openalex", "W1", "10.1234/x", "Provider A title")
    right = paper("s2", "S1", "10.1234/x", "Provider B title")
    left.field_provenance = {"title": [left_provenance]}
    right.field_provenance = {"title": [right_provenance]}
    # Re-validation exercises the same claim synthesis used by provider mappers.
    left = Paper.model_validate(left.model_dump())
    right = Paper.model_validate(right.model_dump())

    merged = merge_exact_records([left, right])[0]

    assert {claim.value for claim in merged.field_claims["title"]} == {
        "Provider A title",
        "Provider B title",
    }
    assert {claim.provenance.provider for claim in merged.field_claims["title"]} == {
        "openalex",
        "s2",
    }


def test_title_only_is_not_auto_merged() -> None:
    resolution = resolve_identities(
        [paper("openalex", "W1", None, "Same Title"), paper("s2", "S1", None, "Same Title")]
    )
    assert len(resolution.papers) == 2
    assert resolution.decisions[0].decision == IdentityDecisionKind.REVIEW


def test_similar_titles_with_different_dois_are_cannot_link() -> None:
    resolution = resolve_identities(
        [
            paper("openalex", "W1", "10.1234/a", "Nearly Identical Title", year=2024),
            paper("s2", "S1", "10.1234/b", "Nearly Identical Title", year=2024),
        ]
    )

    assert len(resolution.papers) == 2
    assert resolution.decisions[0].decision == IdentityDecisionKind.CANNOT_LINK
    assert resolution.decisions[0].conflicting_identifiers == {"doi": ["10.1234/a", "10.1234/b"]}


def test_bridge_record_merges_all_strong_id_components() -> None:
    merged = merge_exact_records(
        [
            paper("crossref", "C1", "10.1234/x", "Paper"),
            paper("arxiv", "A1", None, "Paper", arxiv="2401.12345"),
            paper(
                "openalex",
                "W1",
                "10.1234/x",
                "Paper",
                arxiv="2401.12345",
            ),
        ]
    )

    assert len(merged) == 1
    assert {record.provider for record in merged[0].source_records} == {
        "crossref",
        "arxiv",
        "openalex",
    }


def test_transitive_merge_does_not_create_cluster_level_id_conflict() -> None:
    resolution = resolve_identities(
        [
            paper("p1", "1", "10.1234/x", "Paper"),
            paper("p2", "2", None, "Paper", arxiv="2401.12345"),
            paper("p3", "3", "10.1234/x", "Paper", pmid="111"),
            paper("p4", "4", None, "Paper", arxiv="2401.12345", pmid="222"),
            paper(
                "p5",
                "5",
                "10.1234/x",
                "Paper",
                arxiv="2401.12345",
            ),
        ]
    )

    assert len(resolution.papers) == 2
    assert any(
        decision.decision == IdentityDecisionKind.CANNOT_LINK
        and decision.conflicting_identifiers.get("pmid") == ["111", "222"]
        for decision in resolution.decisions
    )


def test_same_provider_record_id_is_a_safe_exact_identity_key() -> None:
    resolution = resolve_identities(
        [
            paper("fixture", "1", None, "First observation"),
            paper("fixture", "1", None, "Updated observation"),
        ]
    )

    assert len(resolution.papers) == 1
    assert resolution.record_id_map == {"fixture:1": "fixture:1"}
    assert resolution.decisions[0].reasons == ["same provider record identifier"]


def test_active_split_event_overrides_automatic_same_doi_merge() -> None:
    event = IdentityEvent(
        event_id="event-split",
        action=IdentityEventAction.SPLIT,
        relation=EntityRelationKind.DIFFERENT_WORK,
        left_record_id="openalex:W1",
        right_record_id="s2:S1",
        reasons=["curator verified distinct works"],
    )
    resolution = resolve_identities(
        [
            paper("openalex", "W1", "10.1234/x", "First"),
            paper("s2", "S1", "10.1234/x", "Second"),
        ],
        events=[event],
    )

    assert len(resolution.papers) == 2
    assert resolution.decisions[0].decision == IdentityDecisionKind.CANNOT_LINK
    assert "event-split" in resolution.decisions[0].reasons[0]


def test_version_of_event_groups_family_without_merging_manifestations() -> None:
    event = IdentityEvent(
        event_id="event-version",
        action=IdentityEventAction.MERGE,
        relation=EntityRelationKind.VERSION_OF,
        left_record_id="arxiv:A1",
        right_record_id="crossref:C1",
        reasons=["accepted manuscript links to version of record"],
    )
    resolution = resolve_identities(
        [
            paper("arxiv", "A1", None, "Preprint"),
            paper("crossref", "C1", "10.1234/final", "Journal version"),
        ],
        events=[event],
    )

    assert len(resolution.papers) == 2
    assert resolution.papers[0].work_family_id is not None
    assert {item.work_family_id for item in resolution.papers} == {
        resolution.papers[0].work_family_id
    }


def test_arxiv_record_merges_with_datacite_arxiv_doi_record() -> None:
    resolution = resolve_identities(
        [
            paper(
                "arxiv",
                "2603.25723v2",
                None,
                "Natural-Language Agent Harnesses",
                arxiv="2603.25723v2",
            ),
            paper(
                "openalex", "W1", "10.48550/arXiv.2603.25723", "Natural-Language Agent Harnesses"
            ),
        ]
    )
    assert len(resolution.papers) == 1
    assert {source.provider for source in resolution.papers[0].source_records} == {
        "arxiv",
        "openalex",
    }
    assert [d.decision for d in resolution.decisions] == [IdentityDecisionKind.MUST_LINK]


def test_versioned_preprint_dois_form_one_work_without_conflict() -> None:
    resolution = resolve_identities(
        [
            paper("crossref", "v1", "10.20944/preprints202604.0428.v1", "Agent Harness: A Survey"),
            paper("crossref", "v2", "10.20944/preprints202604.0428.v2", "Agent Harness: A Survey"),
            paper(
                "crossref", "rs", "10.21203/rs.3.rs-123456/v1", "An unrelated Research Square work"
            ),
            paper(
                "crossref", "rs2", "10.21203/rs.3.rs-123456/v2", "An unrelated Research Square work"
            ),
        ]
    )
    assert len(resolution.papers) == 2
    merged = next(p for p in resolution.papers if p.title.startswith("Agent Harness"))
    assert {claim.value for claim in merged.identifiers} == {
        "10.20944/preprints202604.0428.v1",
        "10.20944/preprints202604.0428.v2",
    }
    assert all(d.decision == IdentityDecisionKind.MUST_LINK for d in resolution.decisions)


def test_distinct_dois_with_similar_titles_still_cannot_link() -> None:
    # A version suffix is the only DOI difference that is treated as one work.
    resolution = resolve_identities(
        [
            paper("crossref", "a", "10.1000/paper.1", "Same Title"),
            paper("crossref", "b", "10.1000/paper.2", "Same Title"),
        ]
    )
    assert len(resolution.papers) == 2
    assert resolution.decisions[0].decision == IdentityDecisionKind.CANNOT_LINK
