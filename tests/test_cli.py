import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from click.utils import strip_ansi
from typer.testing import CliRunner

import scholarly_retrieval.cli as cli_module
from scholarly_retrieval.cli import (
    _audit_for_result,
    _console_safe,
    _json_for_output,
    _reading_for_result,
    _table_for_result,
    app,
)
from scholarly_retrieval.models import (
    Author,
    Paper,
    ProviderContribution,
    ProviderReport,
    ReferenceExtractionResult,
    RunStatus,
    SourceRecord,
)

runner = CliRunner()


def test_json_output_falls_back_to_lossless_escapes_for_legacy_console() -> None:
    rendered = _json_for_output({"author": "Jörg"}, encoding="gbk")

    assert "\\u00f6" in rendered
    assert json.loads(rendered) == {"author": "Jörg"}


def test_providers_command_reports_real_capability_differences() -> None:
    result = runner.invoke(app, ["providers"])

    assert result.exit_code == 0
    assert '"openalex"' in result.stdout
    assert '"crossref"' in result.stdout
    assert '"citations": "count"' in result.stdout
    assert '"arxiv"' in result.stdout


def test_doctor_reports_credential_presence_without_secret_values(monkeypatch) -> None:
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENCITATIONS_ACCESS_TOKEN", "also-must-not-leak")
    monkeypatch.setenv("SCHOLAR_REDIS_URL", "redis://secret-host/0")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert '"api_key_configured": true' in result.stdout
    assert '"access_token_configured": true' in result.stdout
    assert '"redis_configured": true' in result.stdout
    assert "must-not-leak" not in result.stdout
    assert "secret-host" not in result.stdout


def test_doctor_reports_resolved_storage_path(monkeypatch) -> None:
    database = Path.cwd() / f".test-doctor-{uuid4()}.sqlite3"
    try:
        monkeypatch.setenv("SCHOLAR_DB_PATH", str(database))

        result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["configuration"]["storage"] == {
            "enabled": True,
            "mode": "sqlite",
            "database_path": str(database.resolve()),
        }
        assert database.is_file()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{database}{suffix}").unlink(missing_ok=True)


def test_unknown_source_is_a_clear_cli_usage_error() -> None:
    result = runner.invoke(app, ["search", "graph", "--source", "unknown"])

    assert result.exit_code == 2
    assert "unknown providers: unknown" in result.stderr
    assert "Traceback" not in result.stderr


def test_inverted_year_range_is_reported_before_network_io() -> None:
    result = runner.invoke(
        app,
        ["search", "graph", "--year-from", "2025", "--year-to", "2024"],
    )

    assert result.exit_code == 2
    assert "year_from must not be greater than year_to" in result.stderr


def test_graph_expand_help_exposes_all_budget_controls() -> None:
    result = runner.invoke(app, ["graph", "expand", "--help"])
    help_text = strip_ansi(result.stdout)

    assert result.exit_code == 0
    # Argument parsing is exercised by the following W1 validation test. Typer
    # versions differ on whether variadic positional names appear in Rich usage.
    assert "--direction" in help_text
    assert "--depth" in help_text
    assert "--frontier-cap" in help_text
    assert "--top-k" in help_text
    assert "--per-node-limit" in help_text
    assert "--stop-rule" in help_text
    assert "--year-from" in help_text
    assert "--year-to" in help_text
    assert "--work-type" in help_text
    assert "--max-runtime-seconds" in help_text


def test_graph_expand_rejects_inverted_years_without_traceback() -> None:
    result = runner.invoke(
        app,
        ["graph", "expand", "W1", "--year-from", "2025", "--year-to", "2024"],
    )

    assert result.exit_code == 2
    assert "year_from must not be greater than year_to" in result.stderr
    assert "Traceback" not in result.stderr


def test_related_help_exposes_text_seed_feedback_and_rrf_controls() -> None:
    result = runner.invoke(app, ["related", "--help"])
    help_text = strip_ansi(result.stdout)

    assert result.exit_code == 0
    assert "--text" in help_text
    assert "--positive" in help_text
    assert "--negative" in help_text
    assert "--rrf-k" in help_text


def test_search_help_exposes_advanced_filters_and_sort() -> None:
    result = runner.invoke(app, ["search", "--help"])
    help_text = strip_ansi(result.stdout)

    assert result.exit_code == 0
    assert "--title" in help_text
    assert "--abstract" in help_text
    assert "--venue" in help_text
    assert "--field" in help_text
    assert "--min-citations" in help_text
    assert "--sort" in help_text
    assert "--format" in help_text


def test_table_output_renders_papers_and_is_legacy_console_safe() -> None:
    result = SimpleNamespace(
        status="complete",
        truncated=False,
        fingerprint="v1:fixture",
        papers=[
            Paper(
                record_id="openalex:W1",
                title="Über long-form citation retrieval",
                authors=[Author(name="Ada Lovelace")],
                publication_year=2024,
                source_records=[SourceRecord(provider="openalex", source_record_id="W1")],
            )
        ],
    )

    rendered = _table_for_result(result)
    safe = _console_safe(rendered, encoding="ascii")

    assert "status=complete papers=1 fingerprint=v1:fixture" in rendered
    assert "YEAR" in rendered and "RECORD ID" in rendered
    assert "openalex:W1" in rendered
    assert "Ada Lovelace" in rendered
    assert "\\xdcber" in safe


def test_reading_output_lists_all_papers_without_diagnostics() -> None:
    papers = [
        Paper(
            record_id=f"openalex:W{index}",
            title=f"Auditable citation {index}",
            publication_year=2026,
            source_records=[SourceRecord(provider="openalex", source_record_id=f"W{index}")],
        )
        for index in range(25)
    ]
    result = SimpleNamespace(
        operation="citations",
        status=RunStatus.PARTIAL,
        papers=papers,
        raw_record_count=31,
        work_family_count=25,
        provider_reports=[
            ProviderReport(
                provider="openalex",
                operation="citations",
                status=RunStatus.COMPLETE,
                retrieved_count=25,
                total_available=30,
            ),
            ProviderReport(
                provider="semantic_scholar",
                operation="citations",
                status=RunStatus.THROTTLED,
                error_code="http_429",
                error_message="rate limited",
            ),
        ],
        provider_contributions=[
            ProviderContribution(
                provider="openalex",
                raw_record_count=31,
                canonical_record_count=25,
                unique_canonical_count=19,
                overlap_canonical_count=6,
                result_share=1,
            )
        ],
    )

    rendered = _audit_for_result(result)

    assert "25 篇" in rendered
    assert "openalex" in rendered
    assert "http_429" not in rendered
    assert "rate limited" not in rendered
    assert "raw=" not in rendered
    assert "omitted" not in rendered
    assert "Auditable citation 24" in rendered
    assert "2026" not in rendered


def test_citations_defaults_to_compact_audit_output(monkeypatch) -> None:
    class AuditService:
        async def citations(self, _identifier, *, limit, sources):
            assert limit == 100
            assert sources == ["openalex"]
            return SimpleNamespace(
                operation="citations",
                status=RunStatus.COMPLETE,
                papers=[Paper(record_id="openalex:W1", title="Citation Graph")],
                raw_record_count=1,
                work_family_count=1,
                provider_reports=[],
                provider_contributions=[],
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli_module, "ScholarService", AuditService)

    result = runner.invoke(app, ["citations", "W0", "--source", "openalex"])

    assert result.exit_code == 0
    assert "1 篇" in result.stdout
    assert "Citation Graph" in result.stdout
    assert '"provider_reports"' not in result.stdout


def test_reading_modes_preserve_full_titles_and_provider_link_association() -> None:
    title = "A very long paper title " * 8
    paper = Paper(
        record_id="fixture:1",
        title=title,
        authors=[Author(name="Ada Lovelace")],
        publication_year=2026,
        venue="Example Conference",
        landing_page_url="https://example.org/paper",
        source_records=[
            SourceRecord(
                provider="openalex", source_record_id="W1", source_url="https://openalex.org/W1"
            ),
            SourceRecord(
                provider="semantic_scholar",
                source_record_id="s1",
                source_url="https://www.semanticscholar.org/paper/s1",
            ),
        ],
    )
    result = SimpleNamespace(papers=[paper])
    compact = _reading_for_result(result)
    detailed = _reading_for_result(result, detailed=True)
    assert paper.title in compact
    assert "openalex | https://openalex.org/W1" in compact
    assert "semantic_scholar | https://www.semanticscholar.org/paper/s1" in compact
    assert "Ada Lovelace" not in compact
    assert "Ada Lovelace" in detailed
    assert "2026" in detailed
    assert "Example Conference" in detailed
    assert "0 篇" in _reading_for_result(SimpleNamespace(papers=[]))


def test_search_table_option_uses_shared_library_result(monkeypatch) -> None:
    class TableService:
        async def search(self, _query, *, sources):
            assert sources == ["openalex"]
            return SimpleNamespace(
                status="complete",
                truncated=False,
                fingerprint="v1:table",
                papers=[Paper(record_id="openalex:W1", title="Citation Graph")],
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli_module, "ScholarService", TableService)

    result = runner.invoke(
        app,
        ["search", "citation graph", "--source", "openalex", "--format", "table"],
    )

    assert result.exit_code == 0
    assert "status=complete papers=1 fingerprint=v1:table" in result.stdout
    assert "Citation Graph" in result.stdout
    assert '"papers"' not in result.stdout


def test_search_defaults_to_reading_output_with_query_header(monkeypatch) -> None:
    class ReadingService:
        async def search(self, query, *, sources):
            return SimpleNamespace(
                status="complete",
                truncated=False,
                fingerprint="v1:reading",
                query=query,
                papers=[
                    Paper(
                        record_id="openalex:W1",
                        title="A very long title that a table would clip but reading mode keeps",
                        landing_page_url="https://doi.org/10.1000/x",
                        source_records=[
                            SourceRecord(
                                provider="openalex",
                                source_record_id="W1",
                                source_url="https://openalex.org/W1",
                            ),
                            SourceRecord(
                                provider="arxiv",
                                source_record_id="2401.00001",
                                source_url="https://arxiv.org/abs/2401.00001",
                            ),
                        ],
                    )
                ],
            )

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli_module, "ScholarService", ReadingService)

    result = runner.invoke(app, ["search", "citation graph", "--source", "openalex"])

    assert result.exit_code == 0
    assert "本次检索去重后论文：1 篇" in result.stdout
    assert "检索词：citation graph" in result.stdout
    assert "1. A very long title that a table would clip but reading mode keeps" in result.stdout
    assert "论文链接：https://doi.org/10.1000/x" in result.stdout
    assert "来源：openalex | https://openalex.org/W1" in result.stdout
    assert "来源：arxiv | https://arxiv.org/abs/2401.00001" in result.stdout
    assert "RECORD ID" not in result.stdout and '"papers"' not in result.stdout


def test_query_plan_is_offline_and_discloses_filter_execution() -> None:
    result = runner.invoke(
        app,
        [
            "query-plan",
            "citation graph",
            "--source",
            "openalex,crossref",
            "--open-access",
            "--title",
            "retrieval",
            "--limit",
            "4",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    plans = {item["provider"]: item for item in payload["plans"]}
    assert plans["openalex"]["execution"]["open_access"] == "provider"
    assert plans["crossref"]["execution"]["open_access"] == "unsupported"
    assert plans["crossref"]["execution"]["title"] == "local"
    assert plans["openalex"]["provider_limit"] == 12


def test_extract_references_infers_external_bibtex(monkeypatch) -> None:
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (
            "@article{alpha, title={Alpha Paper}, year={2024}, doi={10.1234/ALPHA}}"
        ),
    )

    result = runner.invoke(app, ["extract-references", "references.bib"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_format"] == "bibtex"
    assert payload["references"][0]["reference_id"] == "alpha"
    assert payload["references"][0]["evidence_level"] == "bibliography_only"


def test_extract_references_routes_scanned_pdf_through_ocr(monkeypatch) -> None:
    monkeypatch.setattr(Path, "read_bytes", lambda *_args, **_kwargs: b"%PDF-scanned")

    async def fake_extract(
        pdf_bytes: bytes,
        *,
        grobid_url: str,
        include_uncited: bool,
        ocrmypdf_command: str,
        ocr_language: str | None,
    ) -> ReferenceExtractionResult:
        assert pdf_bytes == b"%PDF-scanned"
        assert grobid_url == "http://grobid.test"
        assert include_uncited is False
        assert ocrmypdf_command == "custom-ocr"
        assert ocr_language == "eng+chi_sim"
        return ReferenceExtractionResult(
            source_format="ocr-pdf-grobid-tei",
            references=[],
            bibliography_entry_count=0,
            cited_entry_count=0,
            processing_steps=["ocrmypdf", "grobid", "tei"],
        )

    monkeypatch.setattr(
        cli_module,
        "extract_scanned_pdf_references_with_ocr_and_grobid",
        fake_extract,
    )
    result = runner.invoke(
        app,
        [
            "extract-references",
            "scan.pdf",
            "--grobid-url",
            "http://grobid.test",
            "--ocr",
            "--ocr-language",
            "eng+chi_sim",
            "--ocrmypdf-command",
            "custom-ocr",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["source_format"] == "ocr-pdf-grobid-tei"
    assert payload["processing_steps"] == ["ocrmypdf", "grobid", "tei"]
