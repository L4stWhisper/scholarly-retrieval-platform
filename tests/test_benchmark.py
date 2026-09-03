import pytest

from scholarly_retrieval.benchmark import GoldDataset, evaluate_gold_dataset


def _dataset() -> GoldDataset:
    return GoldDataset(
        dataset_id="fixture-cross-domain",
        dataset_version="2026.09",
        license="CC0 fixture labels",
        annotation_protocol="Two curators adjudicated binary relevance.",
        topics=[
            {
                "topic_id": "bio-1",
                "domain": "biomedicine",
                "query": "example biomedical query",
                "relevant_record_ids": ["A", "B"],
                "cutoff_date": "2025-12-31",
                "provenance": "synthetic unit-test fixture",
            },
            {
                "topic_id": "cs-1",
                "domain": "computer science",
                "query": "example computing query",
                "relevant_record_ids": ["C"],
                "provenance": "synthetic unit-test fixture",
            },
        ],
    )


def test_versioned_gold_suite_reports_deterministic_macro_intervals() -> None:
    first = evaluate_gold_dataset(
        _dataset(),
        {"bio-1": ["A", "X"], "cs-1": ["C"]},
        k=2,
        bootstrap_samples=100,
        random_seed=7,
    )
    second = evaluate_gold_dataset(
        _dataset(),
        {"bio-1": ["A", "X"], "cs-1": ["C"]},
        k=2,
        bootstrap_samples=100,
        random_seed=7,
    )

    assert first == second
    assert first["dataset_version"] == "2026.09"
    assert first["topic_count"] == 2
    assert first["macro"]["recall"]["mean"] == 0.75
    assert first["macro"]["recall"]["ci95"] == [0.5, 1.0]


def test_gold_suite_rejects_missing_or_unknown_topics() -> None:
    with pytest.raises(ValueError, match="missing ranked topics"):
        evaluate_gold_dataset(_dataset(), {"bio-1": []})
    with pytest.raises(ValueError, match="unknown ranked topics"):
        evaluate_gold_dataset(
            _dataset(),
            {"bio-1": [], "cs-1": [], "extra": []},
        )
