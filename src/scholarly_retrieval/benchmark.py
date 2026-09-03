"""Versioned, provenance-bearing gold sets and deterministic suite evaluation."""

from __future__ import annotations

import random
from statistics import fmean

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .evaluation import retrieval_metrics


class GoldTopic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str
    domain: str
    query: str
    relevant_record_ids: list[str] = Field(min_length=1)
    cutoff_date: str | None = None
    provenance: str

    @field_validator("topic_id", "domain", "query", "provenance")
    @classmethod
    def required_text_must_not_be_blank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("gold topic text fields must not be blank")
        return normalized

    @field_validator("relevant_record_ids")
    @classmethod
    def relevant_ids_must_be_unique(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("gold relevant IDs must not contain blanks")
        if len(set(normalized)) != len(normalized):
            raise ValueError("gold relevant IDs must be unique")
        return normalized


class GoldDataset(BaseModel):
    """A redistributable manifest; licenses apply to labels, not provider dumps."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1"
    dataset_id: str
    dataset_version: str
    license: str
    annotation_protocol: str
    topics: list[GoldTopic] = Field(min_length=1)

    @field_validator("dataset_id", "dataset_version", "license", "annotation_protocol")
    @classmethod
    def manifest_text_must_not_be_blank(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("gold dataset manifest fields must not be blank")
        return normalized


def evaluate_gold_dataset(
    dataset: GoldDataset,
    ranked_by_topic: dict[str, list[str]],
    *,
    k: int | None = None,
    bootstrap_samples: int = 1000,
    random_seed: int = 0,
) -> dict:
    """Evaluate every topic and report deterministic macro bootstrap intervals."""

    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be at least 1")
    expected = {topic.topic_id for topic in dataset.topics}
    missing = sorted(expected - set(ranked_by_topic))
    unknown = sorted(set(ranked_by_topic) - expected)
    if missing:
        raise ValueError(f"missing ranked topics: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"unknown ranked topics: {', '.join(unknown)}")

    per_topic = {
        topic.topic_id: {
            "domain": topic.domain,
            **retrieval_metrics(
                ranked_by_topic[topic.topic_id],
                set(topic.relevant_record_ids),
                k=k,
            ),
        }
        for topic in dataset.topics
    }
    metric_names = ("precision", "recall", "mrr", "map", "ndcg")
    macro = {}
    for metric in metric_names:
        values = [float(per_topic[topic.topic_id][metric]) for topic in dataset.topics]
        low, high = bootstrap_mean_interval(
            values, samples=bootstrap_samples, random_seed=random_seed
        )
        macro[metric] = {"mean": fmean(values), "ci95": [low, high]}
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "topic_count": len(dataset.topics),
        "k": k,
        "per_topic": per_topic,
        "macro": macro,
        "bootstrap_samples": bootstrap_samples,
        "random_seed": random_seed,
    }


def bootstrap_mean_interval(
    values: list[float], *, samples: int = 1000, random_seed: int = 0
) -> tuple[float, float]:
    if not values:
        raise ValueError("bootstrap requires at least one value")
    generator = random.Random(random_seed)
    size = len(values)
    estimates = sorted(
        fmean(values[generator.randrange(size)] for _ in range(size)) for _ in range(samples)
    )
    low_index = max(0, int(0.025 * samples) - 1)
    high_index = min(samples - 1, int(0.975 * samples))
    return estimates[low_index], estimates[high_index]
