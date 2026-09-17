"""Human- and script-friendly command line adapter."""

from __future__ import annotations

import asyncio
import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer

from .benchmark import GoldDataset, evaluate_gold_dataset
from .evaluation import (
    b_cubed_metrics,
    ceaf_e_metrics,
    exact_cluster_metrics,
    pairwise_cluster_metrics,
    relation_confusion_metrics,
    retrieval_metrics,
    work_saved_at_recall,
)
from .exporters import ExportFormat, export_papers
from .models import (
    EntityRelationKind,
    ExpansionDirection,
    GraphExpansionQuery,
    IdentityEventAction,
    Paper,
    ReferenceExtractionResult,
    ReferenceLinkingResult,
    RelatedQuery,
    SearchExpression,
    SearchQuery,
    SearchSort,
    StopRule,
)
from .reference_benchmark import ReferenceGoldDataset, evaluate_reference_gold_dataset
from .reference_extraction import (
    extract_pdf_references_with_grobid,
    extract_references_from_text,
    extract_scanned_pdf_references_with_ocr_and_grobid,
)
from .reference_smoke import fetch_europe_pmc_jats, validate_reference_smoke
from .service import ScholarService

app = typer.Typer(no_args_is_help=True, help="Search papers and traverse citation relations.")
graph_app = typer.Typer(no_args_is_help=True, help="Build bounded local citation graphs.")
review_app = typer.Typer(no_args_is_help=True, help="Audit and roll back identity decisions.")
app.add_typer(graph_app, name="graph")
app.add_typer(review_app, name="review")
DEFAULT_SOURCES = "openalex,semantic_scholar,crossref,arxiv,europe_pmc"


class CliOutputFormat(StrEnum):
    COMPACT = "compact"
    DETAILED = "detailed"
    AUDIT = "audit"
    JSON = "json"
    TABLE = "table"


def _sources(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _json_for_output(payload, *, encoding: str | None = None) -> str:
    """Keep JSON valid on legacy Windows consoles with non-Unicode encodings."""

    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    output_encoding = encoding or getattr(sys.stdout, "encoding", None)
    if output_encoding:
        try:
            rendered.encode(output_encoding)
        except (LookupError, UnicodeEncodeError):
            # JSON escapes are lossless and prevent a late GBK/ASCII write
            # failure after an otherwise successful, potentially costly query.
            return json.dumps(payload, ensure_ascii=True, indent=2)
    return rendered


def _dump(value, *, output_format: CliOutputFormat = CliOutputFormat.JSON) -> None:
    if output_format in (CliOutputFormat.COMPACT, CliOutputFormat.DETAILED):
        typer.echo(
            _console_safe(
                _reading_for_result(value, detailed=output_format == CliOutputFormat.DETAILED)
            )
        )
        return
    if output_format == CliOutputFormat.AUDIT:
        typer.echo(_console_safe(_audit_for_result(value)))
        return
    if output_format == CliOutputFormat.TABLE:
        typer.echo(_console_safe(_table_for_result(value)))
        return
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
    elif isinstance(value, list):
        payload = [item.model_dump(mode="json") for item in value]
    else:
        payload = value
    typer.echo(_json_for_output(payload))


def _console_safe(value: str, *, encoding: str | None = None) -> str:
    """Keep human-readable output printable on legacy Windows consoles."""

    output_encoding = encoding or getattr(sys.stdout, "encoding", None)
    if not output_encoding:
        return value
    try:
        value.encode(output_encoding)
    except (LookupError, UnicodeEncodeError):
        return value.encode(output_encoding, errors="backslashreplace").decode(output_encoding)
    return value


def _table_for_result(value) -> str:
    """Render the common paper result contract without adding a UI dependency."""

    papers = list(getattr(value, "papers", []))
    status = getattr(value, "status", None)
    truncated = getattr(value, "truncated", False)
    fingerprint = getattr(value, "fingerprint", None)
    summary = [f"papers={len(papers)}"]
    if status is not None:
        summary.insert(0, f"status={status}")
    if truncated:
        summary.append("truncated=true")
    if fingerprint:
        summary.append(f"fingerprint={fingerprint}")

    columns = [
        ("#", 4),
        ("YEAR", 6),
        ("TITLE", 48),
        ("AUTHORS", 24),
        ("RECORD ID", 28),
        ("SOURCES", 24),
    ]
    header = " | ".join(label.ljust(width) for label, width in columns)
    separator = "-+-".join("-" * width for _, width in columns)
    rows = [" ".join(summary), header, separator]
    for rank, paper in enumerate(papers, start=1):
        authors = "; ".join(author.name for author in paper.authors)
        sources = "; ".join(dict.fromkeys(source.provider for source in paper.source_records))
        values = [
            str(rank),
            str(paper.publication_year or ""),
            paper.title,
            authors,
            paper.record_id,
            sources,
        ]
        rows.append(
            " | ".join(
                _clip_table_value(cell, width).ljust(width)
                for cell, (_, width) in zip(values, columns, strict=True)
            )
        )
    if not papers:
        rows.append("[no papers]")
    return "\n".join(rows)


def _audit_for_result(value) -> str:
    """Compatibility alias for the former audit view."""
    return _reading_for_result(value)


def _reading_for_result(value, *, detailed: bool = False) -> str:
    """Display every canonical paper; never merge unrelated works by title here.

    Identity resolution belongs to the service. Source URLs stay attached to
    their own provider rather than being replaced by another provider's URL.
    Diagnostic evidence remains in the persisted result and JSON export.
    """
    papers = list(getattr(value, "papers", []))
    rows = [f"本次检索去重后论文：{len(papers)} 篇"]
    seed = getattr(value, "seed", None)
    if seed is not None:
        rows.append(f"目标论文：{seed.title}")
    for rank, paper in enumerate(papers, start=1):
        rows.extend(["", f"{rank}. {paper.title}"])
        if detailed:
            rows.append(f"   年份：{paper.publication_year or '未提供'}")
            rows.append("   作者：" + ("; ".join(a.name for a in paper.authors) or "未提供"))
            if paper.venue:
                rows.append(f"   期刊/会议：{paper.venue}")
        if paper.landing_page_url:
            rows.append(f"   论文链接：{paper.landing_page_url}")
        seen = set()
        for source in paper.source_records:
            key = (source.provider, source.source_url)
            if key in seen:
                continue
            seen.add(key)
            rows.append(f"   来源：{source.provider} | {source.source_url or '未提供链接'}")
        if not paper.source_records:
            rows.append("   来源：未提供")
        if detailed and paper.pdf_url:
            rows.append(f"   PDF：{paper.pdf_url}")
    return "\n".join(rows)


def _clip_table_value(value: str, width: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= width else f"{normalized[: width - 3]}..."


async def _with_service(callback):
    service = ScholarService()
    try:
        return await callback(service)
    finally:
        await service.close()


def _abort_for_user_error(exc: ValueError | LookupError) -> None:
    """Render expected input/configuration failures without a Python traceback."""

    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(code=2) from exc


def _execute(callback, *, output_format: CliOutputFormat = CliOutputFormat.JSON) -> None:
    try:
        _dump(asyncio.run(_with_service(callback)), output_format=output_format)
    except (ValueError, LookupError) as exc:
        _abort_for_user_error(exc)


@app.command()
def providers() -> None:
    """List configured providers and their actual capabilities."""

    async def run(service: ScholarService):
        return service.provider_capabilities()

    _execute(run)


@app.command("query-plan")
def query_plan(
    query: str,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    source: Annotated[str, typer.Option(help="Comma-separated provider names")] = DEFAULT_SOURCES,
    year_from: Annotated[int | None, typer.Option()] = None,
    year_to: Annotated[int | None, typer.Option()] = None,
    author: Annotated[str | None, typer.Option()] = None,
    open_access: Annotated[bool | None, typer.Option()] = None,
    title: Annotated[str | None, typer.Option()] = None,
    abstract: Annotated[str | None, typer.Option()] = None,
    venue: Annotated[str | None, typer.Option()] = None,
    field: Annotated[str | None, typer.Option()] = None,
    work_type: Annotated[list[str] | None, typer.Option("--work-type")] = None,
    min_citations: Annotated[int | None, typer.Option(min=0)] = None,
    sort: Annotated[SearchSort, typer.Option()] = SearchSort.RELEVANCE,
) -> None:
    """Preview source routing and filter execution without network access."""

    try:
        request = SearchQuery(
            text=query,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            author=author,
            open_access=open_access,
            title=title,
            abstract=abstract,
            venue=venue,
            field=field,
            work_types=work_type or [],
            min_citations=min_citations,
            sort=sort,
        )
    except ValueError as exc:
        _abort_for_user_error(exc)

    async def run(service: ScholarService):
        return service.plan_search(request, sources=_sources(source))

    _execute(run)


@app.command()
def search(
    query: str,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    source: Annotated[str, typer.Option(help="Comma-separated provider names")] = DEFAULT_SOURCES,
    year_from: Annotated[int | None, typer.Option()] = None,
    year_to: Annotated[int | None, typer.Option()] = None,
    author: Annotated[str | None, typer.Option()] = None,
    open_access: Annotated[bool | None, typer.Option()] = None,
    title: Annotated[str | None, typer.Option()] = None,
    abstract: Annotated[str | None, typer.Option()] = None,
    venue: Annotated[str | None, typer.Option()] = None,
    field: Annotated[str | None, typer.Option()] = None,
    work_type: Annotated[list[str] | None, typer.Option("--work-type")] = None,
    min_citations: Annotated[int | None, typer.Option(min=0)] = None,
    sort: Annotated[SearchSort, typer.Option()] = SearchSort.RELEVANCE,
    expression: Annotated[
        str | None,
        typer.Option(help="JSON Boolean/phrase SearchExpression AST"),
    ] = None,
    output_format: Annotated[
        CliOutputFormat, typer.Option("--format", help="json or table")
    ] = CliOutputFormat.JSON,
) -> None:
    """Search one or more scholarly providers."""

    try:
        parsed_expression = SearchExpression.model_validate_json(expression) if expression else None
        request = SearchQuery(
            text=query,
            expression=parsed_expression,
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            author=author,
            open_access=open_access,
            title=title,
            abstract=abstract,
            venue=venue,
            field=field,
            work_types=work_type or [],
            min_citations=min_citations,
            sort=sort,
        )
    except ValueError as exc:
        _abort_for_user_error(exc)

    async def run(service: ScholarService):
        return await service.search(request, sources=_sources(source))

    _execute(run, output_format=output_format)


@app.command()
def resolve(
    identifier: str,
    source: Annotated[str, typer.Option()] = DEFAULT_SOURCES,
    output_format: Annotated[
        CliOutputFormat, typer.Option("--format", help="json or table")
    ] = CliOutputFormat.JSON,
) -> None:
    """Resolve a DOI or provider identifier to paper records."""

    async def run(service: ScholarService):
        return await service.resolve(identifier, sources=_sources(source))

    _execute(run, output_format=output_format)


@app.command()
def related(
    text: Annotated[str | None, typer.Option(help="Natural-language semantic query")] = None,
    positive: Annotated[
        list[str] | None,
        typer.Option("--positive", help="Repeatable positive seed identifier"),
    ] = None,
    negative: Annotated[
        list[str] | None,
        typer.Option("--negative", help="Repeatable negative seed identifier"),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=100)] = 20,
    year_from: Annotated[int | None, typer.Option()] = None,
    year_to: Annotated[int | None, typer.Option()] = None,
    open_access: Annotated[bool | None, typer.Option()] = None,
    work_type: Annotated[
        list[str] | None,
        typer.Option("--work-type", help="Repeatable work type filter"),
    ] = None,
    rrf_k: Annotated[int, typer.Option(min=1, max=1000)] = 60,
    source: Annotated[str, typer.Option()] = "openalex,semantic_scholar",
    output_format: Annotated[
        CliOutputFormat, typer.Option("--format", help="json or table")
    ] = CliOutputFormat.JSON,
) -> None:
    """Find related papers with semantic and seed recommendation retrieval."""

    try:
        request = RelatedQuery(
            text=text,
            positive_identifiers=positive or [],
            negative_identifiers=negative or [],
            limit=limit,
            year_from=year_from,
            year_to=year_to,
            open_access=open_access,
            include_work_types=work_type or [],
            rrf_k=rrf_k,
        )
    except ValueError as exc:
        _abort_for_user_error(exc)

    async def run(service: ScholarService):
        return await service.related(request, sources=_sources(source))

    _execute(run, output_format=output_format)


def _graph_command(
    operation: str,
    identifier: str,
    source: str,
    limit: int,
    output_format: CliOutputFormat,
) -> None:
    async def run(service: ScholarService):
        method = getattr(service, operation)
        return await method(identifier, limit=limit, sources=_sources(source))

    _execute(run, output_format=output_format)


@app.command()
def references(
    identifier: str,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    source: Annotated[str, typer.Option()] = DEFAULT_SOURCES,
    output_format: Annotated[
        CliOutputFormat, typer.Option("--format", help="compact or detailed; json for export")
    ] = CliOutputFormat.COMPACT,
) -> None:
    """List works referenced by the seed paper."""

    _graph_command("references", identifier, source, limit, output_format)


@app.command()
def citations(
    identifier: str,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    source: Annotated[str, typer.Option()] = DEFAULT_SOURCES,
    output_format: Annotated[
        CliOutputFormat, typer.Option("--format", help="compact or detailed; json for export")
    ] = CliOutputFormat.COMPACT,
) -> None:
    """List later works citing the seed paper."""

    _graph_command("citations", identifier, source, limit, output_format)


@graph_app.command("expand")
def graph_expand(
    identifiers: list[str],
    direction: Annotated[ExpansionDirection, typer.Option()] = ExpansionDirection.BOTH,
    depth: Annotated[int, typer.Option(min=1, max=3)] = 2,
    frontier_cap: Annotated[int, typer.Option(min=1, max=1000)] = 200,
    top_k: Annotated[int, typer.Option(min=1, max=5000)] = 100,
    per_node_limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    stop_rule: Annotated[StopRule, typer.Option()] = StopRule.BUDGET,
    year_from: Annotated[int | None, typer.Option()] = None,
    year_to: Annotated[int | None, typer.Option()] = None,
    work_type: Annotated[
        list[str] | None,
        typer.Option("--work-type", help="Repeatable candidate work type filter"),
    ] = None,
    max_runtime_seconds: Annotated[float | None, typer.Option(min=0.1, max=3600)] = None,
    source: Annotated[str, typer.Option()] = DEFAULT_SOURCES,
    output_format: Annotated[
        CliOutputFormat, typer.Option("--format", help="json or table")
    ] = CliOutputFormat.JSON,
) -> None:
    """Expand a deterministic local graph under explicit traversal budgets."""

    try:
        request = GraphExpansionQuery(
            identifiers=identifiers,
            direction=direction,
            depth=depth,
            frontier_cap=frontier_cap,
            top_k=top_k,
            per_node_limit=per_node_limit,
            stop_rule=stop_rule,
            year_from=year_from,
            year_to=year_to,
            include_work_types=work_type or [],
            max_runtime_seconds=max_runtime_seconds,
        )
    except ValueError as exc:
        _abort_for_user_error(exc)

    async def run(service: ScholarService):
        return await service.expand(request, sources=_sources(source))

    _execute(run, output_format=output_format)


@app.command()
def doctor() -> None:
    """Show local configuration without printing secrets."""

    async def run(service: ScholarService):
        return {
            "status": "configured",
            "configuration": service.configuration_status(),
            "providers": service.provider_capabilities(),
            "storage": service.storage_stats(),
            "note": (
                "Set SCHOLAR_DB_PATH to enable raw-response audit and HTTP caching. "
                "Live probes remain explicit and rate-limit-aware."
            ),
        }

    _execute(run)


@app.command("maintenance")
def maintenance_command(
    retention_days: Annotated[int, typer.Option(min=1)] = 30,
    max_raw_responses: Annotated[int | None, typer.Option(min=0)] = None,
    apply: Annotated[
        bool,
        typer.Option(help="Delete eligible rows; without this flag only preview"),
    ] = False,
) -> None:
    """Preview or apply the configured SQLite retention policy."""

    async def run(service: ScholarService):
        return service.storage_maintenance(
            retention_days=retention_days,
            max_raw_responses=max_raw_responses,
            apply=apply,
        )

    _execute(run)


@app.command("citation-evidence")
def citation_evidence_command(edge_id: str) -> None:
    """Replay a persisted citation edge, assertions, and verification upgrades."""

    async def run(service: ScholarService):
        return service.citation_evidence(edge_id)

    _execute(run)


@review_app.command("list")
def review_list(
    active_only: Annotated[
        bool, typer.Option(help="Hide reverted decisions and rollback events")
    ] = False,
) -> None:
    """List persisted human identity decisions in audit order."""

    async def run(service: ScholarService):
        return service.identity_events(active_only=active_only)

    _execute(run)


@review_app.command("decide")
def review_decide(
    left_record_id: str,
    right_record_id: str,
    action: Annotated[IdentityEventAction, typer.Option()] = IdentityEventAction.DEFER,
    relation: Annotated[EntityRelationKind, typer.Option()] = EntityRelationKind.SAME_MANIFESTATION,
    reason: Annotated[
        list[str] | None, typer.Option("--reason", help="Repeatable audit reason")
    ] = None,
) -> None:
    """Persist a merge, split, or deferred identity-review decision."""

    async def run(service: ScholarService):
        return service.record_identity_event(
            action=action,
            relation=relation,
            left_record_id=left_record_id,
            right_record_id=right_record_id,
            reasons=reason or ["manual review"],
        )

    _execute(run)


@review_app.command("revert")
def review_revert(
    event_id: str,
    reason: Annotated[str, typer.Option(help="Why this decision is rolled back")],
) -> None:
    """Append a rollback event while retaining the original audit record."""

    async def run(service: ScholarService):
        return service.revert_identity_event(event_id, reason=reason)

    _execute(run)


@app.command("export")
def export_command(
    input_path: Path,
    output_format: Annotated[ExportFormat, typer.Option("--format")] = ExportFormat.JSONL,
    output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Export papers from a saved result JSON to JSONL, CSV, RIS, or BibTeX."""

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        items = payload.get("papers") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ValueError("input JSON must be a paper list or contain a papers list")
        rendered = export_papers([Paper.model_validate(item) for item in items], output_format)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _abort_for_user_error(exc)
    if output is None:
        typer.echo(rendered, nl=False)
    else:
        output.write_text(rendered, encoding="utf-8", newline="")


@app.command("evaluate")
def evaluate_command(
    retrieved_path: Path,
    gold_path: Path,
    k: Annotated[int | None, typer.Option(min=1)] = None,
    target_recall: Annotated[float, typer.Option(min=0.01, max=1.0)] = 0.95,
) -> None:
    """Evaluate a ranked JSON ID list against a binary-relevance gold list."""

    try:
        retrieved = json.loads(retrieved_path.read_text(encoding="utf-8"))
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        if not isinstance(retrieved, list) or not all(isinstance(item, str) for item in retrieved):
            raise ValueError("retrieved JSON must be a list of record ID strings")
        if not isinstance(gold, list) or not all(isinstance(item, str) for item in gold):
            raise ValueError("gold JSON must be a list of record ID strings")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _abort_for_user_error(exc)
    evaluated = list(dict.fromkeys(retrieved))[:k] if k is not None else retrieved
    _dump(
        {
            **retrieval_metrics(retrieved, set(gold), k=k),
            "work_saved": work_saved_at_recall(
                evaluated,
                set(gold),
                target_recall=target_recall,
            ),
        }
    )


@app.command("evaluate-suite")
def evaluate_suite_command(
    gold_path: Path,
    results_path: Path,
    k: Annotated[int | None, typer.Option(min=1)] = None,
    bootstrap_samples: Annotated[int, typer.Option(min=1)] = 1000,
    random_seed: int = 0,
) -> None:
    """Evaluate a versioned multi-topic gold set with macro 95% intervals."""

    try:
        dataset = GoldDataset.model_validate_json(gold_path.read_text(encoding="utf-8"))
        ranked_by_topic = json.loads(results_path.read_text(encoding="utf-8"))
        if not isinstance(ranked_by_topic, dict) or not all(
            isinstance(topic, str)
            and isinstance(ranked, list)
            and all(isinstance(item, str) for item in ranked)
            for topic, ranked in ranked_by_topic.items()
        ):
            raise ValueError("results JSON must map topic IDs to ranked ID lists")
        result = evaluate_gold_dataset(
            dataset,
            ranked_by_topic,
            k=k,
            bootstrap_samples=bootstrap_samples,
            random_seed=random_seed,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _abort_for_user_error(exc)
    _dump(result)


@app.command("evaluate-identity")
def evaluate_identity_command(predicted_path: Path, gold_path: Path) -> None:
    """Evaluate record-to-cluster JSON mappings with B-cubed and pairwise metrics."""

    try:
        predicted = json.loads(predicted_path.read_text(encoding="utf-8"))
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        if not isinstance(predicted, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in predicted.items()
        ):
            raise ValueError("predicted clusters must be a JSON object of string IDs")
        if not isinstance(gold, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in gold.items()
        ):
            raise ValueError("gold clusters must be a JSON object of string IDs")
        result = {
            "b_cubed": b_cubed_metrics(predicted, gold),
            "pairwise": pairwise_cluster_metrics(predicted, gold),
            "ceaf_e": ceaf_e_metrics(predicted, gold),
            "exact_cluster": exact_cluster_metrics(predicted, gold),
        }
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _abort_for_user_error(exc)
    _dump(result)


@app.command("evaluate-relations")
def evaluate_relations_command(predicted_path: Path, gold_path: Path) -> None:
    """Evaluate pair-to-version-relation JSON with a confusion matrix."""

    allowed = [relation.value for relation in EntityRelationKind]
    try:
        predicted = json.loads(predicted_path.read_text(encoding="utf-8"))
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        for label, mapping in (("predicted", predicted), ("gold", gold)):
            if not isinstance(mapping, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
            ):
                raise ValueError(f"{label} relations must be a JSON object of string labels")
        result = relation_confusion_metrics(predicted, gold, labels=allowed)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _abort_for_user_error(exc)
    _dump(result)


@app.command("evaluate-references")
def evaluate_references_command(gold_path: Path, results_path: Path) -> None:
    """Evaluate saved Reference linking results at five pipeline layers."""

    try:
        dataset = ReferenceGoldDataset.model_validate_json(gold_path.read_text(encoding="utf-8"))
        raw_results = json.loads(results_path.read_text(encoding="utf-8"))
        if not isinstance(raw_results, dict) or not all(
            isinstance(document_id, str) and isinstance(result, dict)
            for document_id, result in raw_results.items()
        ):
            raise ValueError(
                "reference results JSON must map document IDs to linking result objects"
            )
        results = {
            document_id: ReferenceLinkingResult.model_validate(result)
            for document_id, result in raw_results.items()
        }
        evaluated = evaluate_reference_gold_dataset(dataset, results)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _abort_for_user_error(exc)
    _dump(evaluated)


@app.command("extract-references")
def extract_references_command(
    input_path: Path,
    input_format: Annotated[
        str | None,
        typer.Option("--format", help="jats, tei, latex, bibtex, or pdf; inferred from suffix"),
    ] = None,
    include_uncited: Annotated[
        bool,
        typer.Option(help="Include bibliography entries without a body callout"),
    ] = False,
    grobid_url: Annotated[
        str | None,
        typer.Option(help="Required for PDF, e.g. http://localhost:8070"),
    ] = None,
    ocr: Annotated[
        bool,
        typer.Option(help="Run local OCRmyPDF before GROBID for scanned/mixed PDFs"),
    ] = False,
    ocr_language: Annotated[
        str | None,
        typer.Option(help="Tesseract language(s), e.g. eng or eng+chi_sim"),
    ] = None,
    ocrmypdf_command: Annotated[
        str,
        typer.Option(help="OCRmyPDF executable name or absolute path"),
    ] = "ocrmypdf",
) -> None:
    """Extract body-verified references from authorized structured text or PDF."""

    try:
        format_name = (input_format or _reference_format_from_suffix(input_path)).casefold()
        if format_name == "pdf":
            if grobid_url is None:
                raise ValueError("--grobid-url is required for PDF extraction")
            pdf_bytes = input_path.read_bytes()
            if ocr:
                result = asyncio.run(
                    extract_scanned_pdf_references_with_ocr_and_grobid(
                        pdf_bytes,
                        grobid_url=grobid_url,
                        include_uncited=include_uncited,
                        ocrmypdf_command=ocrmypdf_command,
                        ocr_language=ocr_language,
                    )
                )
            else:
                result = asyncio.run(
                    extract_pdf_references_with_grobid(
                        pdf_bytes,
                        grobid_url=grobid_url,
                        include_uncited=include_uncited,
                    )
                )
        else:
            if ocr or ocr_language is not None:
                raise ValueError("--ocr and --ocr-language are only valid with PDF input")
            text_input = input_path.read_text(encoding="utf-8")
            result = extract_references_from_text(
                text_input,
                source_format=format_name,
                include_uncited=include_uncited,
            )
    except (OSError, ValueError) as exc:
        _abort_for_user_error(exc)
    _dump(result)


@app.command("link-references")
def link_references_command(
    seed_identifier: str,
    extraction_path: Path,
    source: Annotated[str, typer.Option()] = DEFAULT_SOURCES,
) -> None:
    """Link a saved extraction to papers and emit verified citation edges."""

    try:
        extraction = ReferenceExtractionResult.model_validate_json(
            extraction_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        _abort_for_user_error(exc)

    async def run(service: ScholarService):
        return await service.link_references(seed_identifier, extraction, sources=_sources(source))

    _execute(run)


@app.command("validate-reference-smoke")
def validate_reference_smoke_command(
    seed_identifier: str,
    pmcid: str,
    source: Annotated[str, typer.Option()] = "openalex,crossref,europe_pmc",
    sample_size: Annotated[int, typer.Option(min=1, max=20)] = 5,
    relation_limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
) -> None:
    """Compare public JATS references with live provider graph and DOI linking."""

    async def run(service: ScholarService):
        jats_xml = await fetch_europe_pmc_jats(pmcid)
        return await validate_reference_smoke(
            service,
            seed_identifier=seed_identifier,
            pmcid=pmcid,
            jats_xml=jats_xml,
            sources=_sources(source) or [],
            sample_size=sample_size,
            relation_limit=relation_limit,
        )

    _execute(run)


def _reference_format_from_suffix(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix == ".bib":
        return "bibtex"
    if suffix in {".tex", ".latex"}:
        return "latex"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".tei"} or path.name.casefold().endswith(".tei.xml"):
        return "tei"
    return "jats"


if __name__ == "__main__":
    app()
