from __future__ import annotations

import os
from pathlib import Path

from scholarly_retrieval.config import credential_status, load_environment

FIXTURE_ENV = Path(__file__).parent / "fixtures" / "dotenv-test.env"


def test_dotenv_loads_values_but_never_overrides_process_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "from-process")
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("CROSSREF_MAILTO", raising=False)

    loaded = load_environment(FIXTURE_ENV)

    assert loaded == FIXTURE_ENV.resolve()
    assert credential_status()["semantic_scholar"]["api_key_configured"] is True
    assert credential_status()["google_scholar_serpapi"]["api_key_configured"] is True
    assert credential_status()["crossref"]["contact_configured"] is True
    assert os.environ["SEMANTIC_SCHOLAR_API_KEY"] == "from-process"


def test_missing_dotenv_is_a_safe_noop() -> None:
    assert load_environment(Path(__file__).with_name("missing.env")) is None
