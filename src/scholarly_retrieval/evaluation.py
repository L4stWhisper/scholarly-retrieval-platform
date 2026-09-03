"""Small, dependency-free retrieval metrics with explicit denominators."""

from __future__ import annotations

from itertools import combinations
from math import ceil, log2


def retrieval_metrics(
    ranked_ids: list[str],
    relevant_ids: set[str],
    *,
    k: int | None = None,
) -> dict[str, float | int | None]:
    """Compute binary relevance metrics; duplicate retrieved IDs count once."""

    unique_ranked = list(dict.fromkeys(ranked_ids))
    evaluated = unique_ranked[:k] if k is not None else unique_ranked
    hits = [record_id in relevant_ids for record_id in evaluated]
    relevant_found = sum(hits)
    precision = relevant_found / len(evaluated) if evaluated else 0.0
    recall = relevant_found / len(relevant_ids) if relevant_ids else 0.0
    reciprocal_rank = next((1.0 / rank for rank, hit in enumerate(hits, start=1) if hit), 0.0)
    precision_sum = sum(sum(hits[:rank]) / rank for rank, hit in enumerate(hits, start=1) if hit)
    average_precision = precision_sum / len(relevant_ids) if relevant_ids else 0.0
    dcg = sum((1.0 / log2(rank + 1)) for rank, hit in enumerate(hits, start=1) if hit)
    ideal_hits = min(len(relevant_ids), len(evaluated))
    ideal_dcg = sum(1.0 / log2(rank + 1) for rank in range(1, ideal_hits + 1))
    return {
        "screened": len(evaluated),
        "relevant_total": len(relevant_ids),
        "relevant_found": relevant_found,
        "precision": precision,
        "recall": recall,
        "nnr": len(evaluated) / relevant_found if relevant_found else None,
        "mrr": reciprocal_rank,
        "map": average_precision,
        "ndcg": dcg / ideal_dcg if ideal_dcg else 0.0,
    }


def edge_metrics(
    retrieved_edges: set[tuple[str, str]],
    gold_edges: set[tuple[str, str]],
) -> dict[str, float | int]:
    true_positive = len(retrieved_edges & gold_edges)
    precision = true_positive / len(retrieved_edges) if retrieved_edges else 0.0
    recall = true_positive / len(gold_edges) if gold_edges else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "retrieved": len(retrieved_edges),
        "gold": len(gold_edges),
        "true_positive": true_positive,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def work_saved_at_recall(
    ranked_ids: list[str],
    relevant_ids: set[str],
    *,
    target_recall: float = 0.95,
) -> dict[str, float | int | None]:
    """Report WSS at an explicit target recall and candidate denominator."""

    if not 0 < target_recall <= 1:
        raise ValueError("target_recall must be in (0, 1]")
    unique_ranked = list(dict.fromkeys(ranked_ids))
    required = ceil(target_recall * len(relevant_ids))
    if not relevant_ids:
        return {
            "target_recall": target_recall,
            "candidate_count": len(unique_ranked),
            "required_relevant": 0,
            "screened_at_target": None,
            "wss": None,
        }
    found = 0
    screened_at_target: int | None = None
    for rank, record_id in enumerate(unique_ranked, start=1):
        if record_id in relevant_ids:
            found += 1
            if found >= required:
                screened_at_target = rank
                break
    wss = (
        target_recall - screened_at_target / len(unique_ranked)
        if screened_at_target is not None and unique_ranked
        else None
    )
    return {
        "target_recall": target_recall,
        "candidate_count": len(unique_ranked),
        "required_relevant": required,
        "screened_at_target": screened_at_target,
        "wss": wss,
    }


def b_cubed_metrics(
    predicted_clusters: dict[str, str],
    gold_clusters: dict[str, str],
) -> dict[str, float | int]:
    """Evaluate entity clusters without letting large clusters dominate topics."""

    _require_same_cluster_items(predicted_clusters, gold_clusters)
    if not gold_clusters:
        return {"item_count": 0, "precision": 0.0, "recall": 0.0, "f1": 0.0}
    predicted_members = _cluster_members(predicted_clusters)
    gold_members = _cluster_members(gold_clusters)
    precisions: list[float] = []
    recalls: list[float] = []
    for item, predicted_cluster in predicted_clusters.items():
        predicted = predicted_members[predicted_cluster]
        gold = gold_members[gold_clusters[item]]
        shared = len(predicted & gold)
        precisions.append(shared / len(predicted))
        recalls.append(shared / len(gold))
    precision = sum(precisions) / len(precisions)
    recall = sum(recalls) / len(recalls)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "item_count": len(gold_clusters),
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def pairwise_cluster_metrics(
    predicted_clusters: dict[str, str],
    gold_clusters: dict[str, str],
) -> dict[str, float | int]:
    """Evaluate same-cluster pairs to expose false merges and false splits."""

    _require_same_cluster_items(predicted_clusters, gold_clusters)
    predicted_pairs = _cluster_pairs(predicted_clusters)
    gold_pairs = _cluster_pairs(gold_clusters)
    metrics = edge_metrics(predicted_pairs, gold_pairs)
    return {
        "predicted_pairs": metrics["retrieved"],
        "gold_pairs": metrics["gold"],
        "true_positive_pairs": metrics["true_positive"],
        "false_merge_pairs": len(predicted_pairs - gold_pairs),
        "false_split_pairs": len(gold_pairs - predicted_pairs),
        "precision": metrics["precision"],
        "recall": metrics["recall"],
        "f1": metrics["f1"],
    }


def ceaf_e_metrics(
    predicted_clusters: dict[str, str],
    gold_clusters: dict[str, str],
) -> dict[str, float | int]:
    """Compute entity-based CEAF using optimal one-to-one cluster alignment.

    The entity similarity is ``2 * |P ∩ G| / (|P| + |G|)``.  Unlike greedy
    alignment, the Hungarian assignment remains correct when several predicted
    clusters overlap the same gold cluster.
    """

    _require_same_cluster_items(predicted_clusters, gold_clusters)
    predicted = list(_cluster_members(predicted_clusters).values())
    gold = list(_cluster_members(gold_clusters).values())
    if not predicted and not gold:
        return {
            "predicted_cluster_count": 0,
            "gold_cluster_count": 0,
            "aligned_similarity": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
        }
    similarities = [
        [2 * len(predicted_set & gold_set) / (len(predicted_set) + len(gold_set))
         for gold_set in gold]
        for predicted_set in predicted
    ]
    aligned = _maximum_weight_alignment(similarities)
    precision = aligned / len(predicted) if predicted else 0.0
    recall = aligned / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "predicted_cluster_count": len(predicted),
        "gold_cluster_count": len(gold),
        "aligned_similarity": aligned,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def exact_cluster_metrics(
    predicted_clusters: dict[str, str],
    gold_clusters: dict[str, str],
) -> dict[str, float | int]:
    """Count clusters whose complete membership is reproduced exactly."""

    _require_same_cluster_items(predicted_clusters, gold_clusters)
    predicted = {frozenset(items) for items in _cluster_members(predicted_clusters).values()}
    gold = {frozenset(items) for items in _cluster_members(gold_clusters).values()}
    matches = len(predicted & gold)
    precision = matches / len(predicted) if predicted else 0.0
    recall = matches / len(gold) if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "predicted_cluster_count": len(predicted),
        "gold_cluster_count": len(gold),
        "exact_matches": matches,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        # The design's exact-cluster accuracy is defined against gold clusters;
        # expose the denominator instead of using an ambiguous bare percentage.
        "gold_cluster_accuracy": recall,
    }


def relation_confusion_metrics(
    predicted_relations: dict[str, str],
    gold_relations: dict[str, str],
    *,
    labels: list[str] | None = None,
) -> dict[str, object]:
    """Evaluate one relation label per audited record pair.

    Rows are gold labels and columns are predicted labels. Pair keys are opaque
    but must have exactly the same universe in both inputs.
    """

    _require_same_cluster_items(predicted_relations, gold_relations)
    observed = set(predicted_relations.values()) | set(gold_relations.values())
    ordered_labels = list(dict.fromkeys(labels or sorted(observed)))
    if not ordered_labels or any(not label for label in ordered_labels):
        raise ValueError("relation labels must be non-empty")
    unknown = sorted(observed - set(ordered_labels))
    if unknown:
        raise ValueError(f"unknown relation labels: {', '.join(unknown)}")
    matrix = {
        gold_label: {predicted_label: 0 for predicted_label in ordered_labels}
        for gold_label in ordered_labels
    }
    for pair_id, gold_label in gold_relations.items():
        matrix[gold_label][predicted_relations[pair_id]] += 1

    per_label: dict[str, dict[str, float | int]] = {}
    for label in ordered_labels:
        true_positive = matrix[label][label]
        false_positive = sum(matrix[row][label] for row in ordered_labels if row != label)
        false_negative = sum(matrix[label][column] for column in ordered_labels if column != label)
        precision = (
            true_positive / (true_positive + false_positive)
            if true_positive + false_positive
            else 0.0
        )
        recall = (
            true_positive / (true_positive + false_negative)
            if true_positive + false_negative
            else 0.0
        )
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {
            "support": sum(matrix[label].values()),
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    pair_count = len(gold_relations)
    correct = sum(matrix[label][label] for label in ordered_labels)
    return {
        "pair_count": pair_count,
        "labels": ordered_labels,
        "matrix": matrix,
        "per_label": per_label,
        "accuracy": correct / pair_count if pair_count else 0.0,
        "macro_f1": (
            sum(float(per_label[label]["f1"]) for label in ordered_labels)
            / len(ordered_labels)
        ),
    }


def _require_same_cluster_items(predicted: dict[str, str], gold: dict[str, str]) -> None:
    if set(predicted) != set(gold):
        missing = sorted(set(gold) - set(predicted))
        unknown = sorted(set(predicted) - set(gold))
        details = []
        if missing:
            details.append(f"missing predicted items: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown predicted items: {', '.join(unknown)}")
        raise ValueError("; ".join(details))


def _cluster_members(assignments: dict[str, str]) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {}
    for item, cluster in assignments.items():
        members.setdefault(cluster, set()).add(item)
    return members


def _cluster_pairs(assignments: dict[str, str]) -> set[tuple[str, str]]:
    return {
        pair
        for members in _cluster_members(assignments).values()
        for pair in combinations(sorted(members), 2)
    }


def _maximum_weight_alignment(weights: list[list[float]]) -> float:
    """Maximum rectangular assignment weight via the Hungarian algorithm."""

    if not weights or not weights[0]:
        return 0.0
    matrix = weights
    if len(matrix) > len(matrix[0]):
        matrix = [list(column) for column in zip(*matrix, strict=True)]
    row_count = len(matrix)
    column_count = len(matrix[0])
    row_potential = [0.0] * (row_count + 1)
    column_potential = [0.0] * (column_count + 1)
    matched_row = [0] * (column_count + 1)
    predecessor = [0] * (column_count + 1)
    for row in range(1, row_count + 1):
        matched_row[0] = row
        column = 0
        minimum = [float("inf")] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[column] = True
            active_row = matched_row[column]
            delta = float("inf")
            next_column = 0
            for candidate in range(1, column_count + 1):
                if used[candidate]:
                    continue
                # The standard algorithm minimizes cost; negation maximizes weight.
                reduced = (
                    -matrix[active_row - 1][candidate - 1]
                    - row_potential[active_row]
                    - column_potential[candidate]
                )
                if reduced < minimum[candidate]:
                    minimum[candidate] = reduced
                    predecessor[candidate] = column
                if minimum[candidate] < delta:
                    delta = minimum[candidate]
                    next_column = candidate
            for candidate in range(column_count + 1):
                if used[candidate]:
                    row_potential[matched_row[candidate]] += delta
                    column_potential[candidate] -= delta
                else:
                    minimum[candidate] -= delta
            column = next_column
            if matched_row[column] == 0:
                break
        while True:
            previous = predecessor[column]
            matched_row[column] = matched_row[previous]
            column = previous
            if column == 0:
                break
    return sum(
        matrix[matched_row[column] - 1][column - 1]
        for column in range(1, column_count + 1)
        if matched_row[column]
    )
