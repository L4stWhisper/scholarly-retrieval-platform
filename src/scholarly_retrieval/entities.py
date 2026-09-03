"""Conservative projection from canonical paper views into formal entity layers."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from .models import (
    Artifact,
    ArtifactKind,
    EntityGroupingStatus,
    EntityView,
    Manifestation,
    Paper,
    Version,
    WorkFamily,
)


def build_entity_view(papers: list[Paper]) -> EntityView:
    """Build stable entities without inventing version/family relationships.

    Canonical papers are already exact-identifier reconciled manifestations. A
    provider-supplied ``work_family_id`` is the only current cross-version
    grouping signal; all other records deliberately remain singleton families.
    """

    manifestations: list[Manifestation] = []
    versions: list[Version] = []
    artifacts: list[Artifact] = []
    family_members: dict[str, list[tuple[Paper, Manifestation, Version]]] = defaultdict(list)

    # record_id is stable across input order and contains no presentation-only title.
    for paper in sorted(_unique_papers(papers), key=lambda item: item.record_id):
        manifestation_id = _stable_id("manifestation", paper.record_id)
        family_key = paper.work_family_id or f"singleton:{paper.record_id}"
        work_family_id = _stable_id("work-family", family_key)
        version_id = _stable_id("version", paper.record_id)
        manifestation = Manifestation(
            manifestation_id=manifestation_id,
            paper_record_id=paper.record_id,
            version_id=version_id,
            work_family_id=work_family_id,
            source_record_ids=sorted({source.source_record_id for source in paper.source_records}),
        )
        version = Version(
            version_id=version_id,
            work_family_id=work_family_id,
            manifestation_ids=[manifestation_id],
            grouping_status=(
                EntityGroupingStatus.ASSERTED
                if paper.work_family_id
                else EntityGroupingStatus.SINGLETON
            ),
            evidence=(
                [f"provider work_family_id:{paper.work_family_id}"]
                if paper.work_family_id
                else ["no cross-version evidence; retained as singleton"]
            ),
        )
        manifestations.append(manifestation)
        versions.append(version)
        for kind, url in (
            (ArtifactKind.LANDING_PAGE, paper.landing_page_url),
            (ArtifactKind.PDF, paper.pdf_url),
        ):
            if url:
                artifacts.append(
                    Artifact(
                        artifact_id=_stable_id(
                            "artifact", f"{manifestation_id}\0{kind.value}\0{url}"
                        ),
                        manifestation_id=manifestation_id,
                        kind=kind,
                        url=url,
                        open_access=paper.open_access,
                    )
                )
        family_members[work_family_id].append((paper, manifestation, version))

    work_families = []
    for family_id, members in sorted(family_members.items()):
        asserted = any(paper.work_family_id for paper, _, _ in members)
        work_families.append(
            WorkFamily(
                work_family_id=family_id,
                title=members[0][0].title,
                version_ids=[version.version_id for _, _, version in members],
                manifestation_ids=[
                    manifestation.manifestation_id for _, manifestation, _ in members
                ],
                grouping_status=(
                    EntityGroupingStatus.ASSERTED if asserted else EntityGroupingStatus.SINGLETON
                ),
                evidence=(
                    [f"provider work_family_id:{members[0][0].work_family_id}"]
                    if asserted
                    else ["no work-family evidence; retained as singleton"]
                ),
            )
        )
    return EntityView(
        manifestations=manifestations,
        versions=versions,
        work_families=work_families,
        artifacts=artifacts,
    )


def _stable_id(namespace: str, value: str) -> str:
    digest = hashlib.sha256(f"{namespace}\0{value}".encode()).hexdigest()[:24]
    return f"{namespace}:{digest}"


def _unique_papers(papers: list[Paper]) -> list[Paper]:
    by_id: dict[str, Paper] = {}
    for paper in papers:
        by_id.setdefault(paper.record_id, paper)
    return list(by_id.values())
