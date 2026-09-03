from __future__ import annotations

import csv
import io
import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from scholarly_retrieval.cli import app
from scholarly_retrieval.evaluation import (
    b_cubed_metrics,
    ceaf_e_metrics,
    edge_metrics,
    exact_cluster_metrics,
    pairwise_cluster_metrics,
    relation_confusion_metrics,
    retrieval_metrics,
    work_saved_at_recall,
)
from scholarly_retrieval.exporters import ExportFormat, export_papers
from scholarly_retrieval.models import (
    Author,
    IdentifierClaim,
    IdentifierScheme,
    Paper,
    Provenance,
)

runner = CliRunner()


def sample_paper() -> Paper:
    provenance = Provenance(provider="fixture", source_record_id="1")
    return Paper(
        record_id="fixture:1",
        title="Evidence, Retrieval & Graphs",
        authors=[Author(name="Ada Lovelace")],
        publication_year=2024,
        venue="Journal of Tests",
        landing_page_url="https://example.org/paper",
        identifiers=[
            IdentifierClaim(
                scheme=IdentifierScheme.DOI,
                value="10.1234/example",
                provenance=provenance,
            )
        ],
    )


def test_all_export_formats_preserve_core_bibliographic_fields() -> None:
    paper = sample_paper()
    jsonl = export_papers([paper], ExportFormat.JSONL)
    csv_text = export_papers([paper], ExportFormat.CSV)
    ris = export_papers([paper], ExportFormat.RIS)
    bibtex = export_papers([paper], ExportFormat.BIBTEX)

    assert json.loads(jsonl)["record_id"] == "fixture:1"
    row = next(csv.DictReader(io.StringIO(csv_text)))
    assert row["title"] == paper.title
    assert row["doi"] == "10.1234/example"
    assert "DO  - 10.1234/example" in ris
    assert "author = {Ada Lovelace}" in bibtex
    assert export_papers([paper], ExportFormat.BIBTEX) == bibtex


def test_retrieval_and_edge_metrics_have_explicit_denominators() -> None:
    metrics = retrieval_metrics(["A", "A", "B", "C"], {"A", "C", "D"})
    edges = edge_metrics({("A", "B"), ("B", "C")}, {("A", "B"), ("C", "D")})

    assert metrics["screened"] == 3
    assert metrics["relevant_found"] == 2
    assert metrics["recall"] == 2 / 3
    assert metrics["nnr"] == 1.5
    assert edges["precision"] == 0.5
    assert edges["recall"] == 0.5
    assert edges["f1"] == 0.5


def test_work_saved_and_identity_metrics_have_explicit_denominators() -> None:
    ranked = ["N1", "A", "N2", "N3", "N4", "N5", "N6", "B", "N7", "N8"]
    wss = work_saved_at_recall(ranked, {"A", "B"}, target_recall=0.95)
    predicted = {"A": "p1", "B": "p2", "C": "p1"}
    gold = {"A": "g1", "B": "g1", "C": "g2"}
    b_cubed = b_cubed_metrics(predicted, gold)
    pairwise = pairwise_cluster_metrics(predicted, gold)
    ceaf_e = ceaf_e_metrics(predicted, gold)
    exact = exact_cluster_metrics(predicted, gold)

    assert wss["candidate_count"] == 10
    assert wss["screened_at_target"] == 8
    assert wss["wss"] == pytest.approx(0.15)
    assert b_cubed["precision"] == pytest.approx(2 / 3)
    assert b_cubed["recall"] == pytest.approx(2 / 3)
    assert pairwise["false_merge_pairs"] == 1
    assert pairwise["false_split_pairs"] == 1
    assert pairwise["f1"] == 0.0
    assert ceaf_e["precision"] == pytest.approx(2 / 3)
    assert ceaf_e["recall"] == pytest.approx(2 / 3)
    assert exact["exact_matches"] == 0
    assert exact["gold_cluster_accuracy"] == 0.0


def test_identity_metrics_reject_mismatched_item_universes() -> None:
    with pytest.raises(ValueError, match="missing predicted items: B"):
        b_cubed_metrics({"A": "p1"}, {"A": "g1", "B": "g1"})


def test_relation_confusion_matrix_reports_each_version_relation() -> None:
    predicted = {
        "A|B": "same_manifestation",
        "A|C": "different_work",
        "B|C": "version_of",
    }
    gold = {
        "A|B": "same_manifestation",
        "A|C": "version_of",
        "B|C": "version_of",
    }
    metrics = relation_confusion_metrics(
        predicted,
        gold,
        labels=["same_manifestation", "version_of", "different_work"],
    )

    assert metrics["pair_count"] == 3
    assert metrics["accuracy"] == pytest.approx(2 / 3)
    assert metrics["matrix"]["version_of"]["different_work"] == 1
    assert metrics["per_label"]["version_of"]["recall"] == 0.5


def test_export_and_evaluate_cli_work_with_saved_json() -> None:
    saved_result = json.dumps({"papers": [sample_paper().model_dump(mode="json")]})
    with patch("pathlib.Path.read_text", return_value=saved_result):
        exported = runner.invoke(app, ["export", "result.json", "--format", "ris"])

    with patch(
        "pathlib.Path.read_text",
        side_effect=['["A", "B"]', '["B", "C"]'],
    ):
        evaluated = runner.invoke(app, ["evaluate", "retrieved.json", "gold.json"])

    assert exported.exit_code == 0
    assert "TI  - Evidence, Retrieval & Graphs" in exported.stdout
    assert evaluated.exit_code == 0
    payload = json.loads(evaluated.stdout)
    assert payload["recall"] == 0.5
    assert payload["work_saved"]["target_recall"] == 0.95


def test_evaluate_identity_cli_uses_saved_cluster_mappings() -> None:
    with patch(
        "pathlib.Path.read_text",
        side_effect=['{"A":"p1","B":"p2"}', '{"A":"g1","B":"g1"}'],
    ):
        evaluated = runner.invoke(app, ["evaluate-identity", "predicted.json", "gold.json"])

    assert evaluated.exit_code == 0
    payload = json.loads(evaluated.stdout)
    assert payload["b_cubed"]["recall"] == 0.5
    assert payload["pairwise"]["false_split_pairs"] == 1
    assert payload["ceaf_e"]["recall"] == pytest.approx(2 / 3)
    assert payload["exact_cluster"]["gold_cluster_accuracy"] == 0.0


def test_evaluate_relations_cli_uses_saved_pair_mappings() -> None:
    with patch(
        "pathlib.Path.read_text",
        side_effect=[
            '{"A|B":"same_manifestation","A|C":"different_work"}',
            '{"A|B":"same_manifestation","A|C":"version_of"}',
        ],
    ):
        evaluated = runner.invoke(app, ["evaluate-relations", "predicted.json", "gold.json"])

    assert evaluated.exit_code == 0
    payload = json.loads(evaluated.stdout)
    assert payload["accuracy"] == 0.5
    assert payload["matrix"]["version_of"]["different_work"] == 1
