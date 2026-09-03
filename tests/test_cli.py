import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from typer.testing import CliRunner

import scholarly_retrieval.cli as cli_module
from scholarly_retrieval.cli import _console_safe, _json_for_output, _table_for_result, app
from scholarly_retrieval.models import Author, Paper, ReferenceExtractionResult, SourceRecord

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

    assert result.exit_code == 0
    assert "IDENTIFIERS..." in result.stdout
    assert "--direction" in result.stdout
    assert "--depth" in result.stdout
    assert "--frontier-cap" in result.stdout
    assert "--top-k" in result.stdout
    assert "--per-node-limit" in result.stdout
    assert "--stop-rule" in result.stdout
    assert "--year-from" in result.stdout
    assert "--year-to" in result.stdout
    assert "--work-type" in result.stdout
    assert "--max-runtime-seconds" in result.stdout


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

    assert result.exit_code == 0
    assert "--text" in result.stdout
    assert "--positive" in result.stdout
    assert "--negative" in result.stdout
    assert "--rrf-k" in result.stdout


def test_search_help_exposes_advanced_filters_and_sort() -> None:
    result = runner.invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    assert "--title" in result.stdout
    assert "--abstract" in result.stdout
    assert "--venue" in result.stdout
    assert "--field" in result.stdout
    assert "--min-citations" in result.stdout
    assert "--sort" in result.stdout
    assert "--format" in result.stdout


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
