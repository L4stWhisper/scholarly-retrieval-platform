"""Deterministic paper exports for review and reference-manager workflows."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from enum import StrEnum

from .models import IdentifierScheme, Paper


class ExportFormat(StrEnum):
    JSONL = "jsonl"
    CSV = "csv"
    RIS = "ris"
    BIBTEX = "bibtex"


def export_papers(papers: list[Paper], output_format: ExportFormat) -> str:
    """Serialize canonical papers without discarding source-neutral identifiers."""

    if output_format == ExportFormat.JSONL:
        return "".join(
            json.dumps(paper.model_dump(mode="json"), ensure_ascii=False) + "\n" for paper in papers
        )
    if output_format == ExportFormat.CSV:
        return _csv(papers)
    if output_format == ExportFormat.RIS:
        return _ris(papers)
    if output_format == ExportFormat.BIBTEX:
        return _bibtex(papers)
    raise ValueError(f"unsupported export format: {output_format}")


def _identifier(paper: Paper, scheme: IdentifierScheme) -> str | None:
    claim = next((item for item in paper.identifiers if item.scheme == scheme), None)
    return claim.value if claim else None


def _csv(papers: list[Paper]) -> str:
    stream = io.StringIO(newline="")
    fieldnames = [
        "record_id",
        "title",
        "authors",
        "publication_year",
        "venue",
        "doi",
        "url",
        "providers",
    ]
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for paper in papers:
        writer.writerow(
            {
                "record_id": paper.record_id,
                "title": paper.title,
                "authors": "; ".join(author.name for author in paper.authors),
                "publication_year": paper.publication_year or "",
                "venue": paper.venue or "",
                "doi": _identifier(paper, IdentifierScheme.DOI) or "",
                "url": paper.landing_page_url or "",
                "providers": "; ".join(
                    dict.fromkeys(source.provider for source in paper.source_records)
                ),
            }
        )
    return stream.getvalue()


def _ris(papers: list[Paper]) -> str:
    records: list[str] = []
    for paper in papers:
        lines = ["TY  - JOUR", f"TI  - {paper.title}"]
        lines.extend(f"AU  - {author.name}" for author in paper.authors)
        if paper.publication_year:
            lines.append(f"PY  - {paper.publication_year}")
        if paper.venue:
            lines.append(f"JO  - {paper.venue}")
        doi = _identifier(paper, IdentifierScheme.DOI)
        if doi:
            lines.append(f"DO  - {doi}")
        if paper.landing_page_url:
            lines.append(f"UR  - {paper.landing_page_url}")
        lines.extend([f"ID  - {paper.record_id}", "ER  - "])
        records.append("\n".join(lines))
    return "\n\n".join(records) + ("\n" if records else "")


def _bibtex(papers: list[Paper]) -> str:
    records: list[str] = []
    for paper in papers:
        first_author = paper.authors[0].name.rsplit(" ", 1)[-1] if paper.authors else "anon"
        stem = re.sub(r"[^A-Za-z0-9]", "", first_author).casefold() or "paper"
        year = paper.publication_year or "nd"
        suffix = hashlib.sha1(paper.record_id.encode()).hexdigest()[:6]
        key = f"{stem}{year}{suffix}"
        fields = [f"  title = {{{paper.title}}}"]
        if paper.authors:
            fields.append(f"  author = {{{' and '.join(a.name for a in paper.authors)}}}")
        if paper.publication_year:
            fields.append(f"  year = {{{paper.publication_year}}}")
        if paper.venue:
            fields.append(f"  journal = {{{paper.venue}}}")
        doi = _identifier(paper, IdentifierScheme.DOI)
        if doi:
            fields.append(f"  doi = {{{doi}}}")
        if paper.landing_page_url:
            fields.append(f"  url = {{{paper.landing_page_url}}}")
        records.append(f"@article{{{key},\n" + ",\n".join(fields) + "\n}")
    return "\n\n".join(records) + ("\n" if records else "")
