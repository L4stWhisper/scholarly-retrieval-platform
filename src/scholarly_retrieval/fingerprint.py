"""Deterministic fingerprints for replay and regression comparison."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

FINGERPRINT_VERSION = "v1"
VOLATILE_FIELDS = frozenset({"fingerprint", "retrieved_at"})


def stable_fingerprint(value: BaseModel | dict[str, Any]) -> str:
    """Hash semantic output while retaining provider/ranking list order.

    Retrieval timestamps describe when evidence was observed, not what was
    observed. Excluding them makes a fixture replay comparable across runs.
    """

    raw = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    canonical = _without_volatile_fields(raw)
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(encoded).hexdigest()}"


def _without_volatile_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile_fields(item)
            for key, item in value.items()
            if key not in VOLATILE_FIELDS
        }
    if isinstance(value, list):
        # Result order is meaningful (provider rank and graph traversal order),
        # so only mapping keys are sorted by JSON serialization.
        return [_without_volatile_fields(item) for item in value]
    return value
