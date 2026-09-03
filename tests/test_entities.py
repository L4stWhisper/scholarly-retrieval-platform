from scholarly_retrieval.entities import build_entity_view
from scholarly_retrieval.models import EntityGroupingStatus, Paper, SourceRecord


def _paper(record_id: str, *, family: str | None = None) -> Paper:
    return Paper(
        record_id=record_id,
        work_family_id=family,
        title=f"Title {record_id}",
        landing_page_url=f"https://example.org/{record_id}",
        pdf_url=f"https://example.org/{record_id}.pdf",
        open_access=True,
        source_records=[SourceRecord(provider="fixture", source_record_id=f"source:{record_id}")],
    )


def test_entity_view_groups_only_explicit_work_families() -> None:
    view = build_entity_view(
        [_paper("paper:a", family="family:1"), _paper("paper:b", family="family:1")]
    )

    assert len(view.manifestations) == 2
    assert len(view.versions) == 2
    assert len(view.work_families) == 1
    assert len(view.artifacts) == 4
    assert {artifact.kind for artifact in view.artifacts} == {"landing_page", "pdf"}
    family = view.work_families[0]
    assert family.grouping_status == EntityGroupingStatus.ASSERTED
    assert len(family.manifestation_ids) == 2


def test_entity_view_retains_unknown_families_as_stable_singletons() -> None:
    papers = [_paper("paper:b"), _paper("paper:a")]
    first = build_entity_view(papers)
    second = build_entity_view(list(reversed(papers)))

    assert len(first.work_families) == 2
    assert all(
        family.grouping_status == EntityGroupingStatus.SINGLETON for family in first.work_families
    )
    assert first.model_dump() == second.model_dump()
    assert first.manifestations[0].source_record_ids == ["source:paper:a"]
