from __future__ import annotations

import os
from pathlib import Path

from scholarly_retrieval.config import (
    credential_status,
    environment_file_candidates,
    load_environment,
)

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


def test_env_file_search_order_prefers_explicit_then_cwd_then_user_config(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("SCHOLAR_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    candidates = environment_file_candidates()
    assert candidates[0] == tmp_path / ".env"
    assert candidates[1] == tmp_path / "home" / ".config" / "scholarly-retrieval" / ".env"

    monkeypatch.setenv("SCHOLAR_ENV_FILE", str(tmp_path / "explicit.env"))
    assert environment_file_candidates() == [tmp_path / "explicit.env"]


def test_user_level_env_file_is_loaded_when_project_file_is_absent(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("SCHOLAR_ENV_FILE", raising=False)
    monkeypatch.delenv("OPENCITATIONS_ACCESS_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    user_env = tmp_path / "home" / ".config" / "scholarly-retrieval" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("OPENCITATIONS_ACCESS_TOKEN=from-user-file\n", encoding="utf-8")

    assert load_environment() == user_env.resolve()
    assert os.environ["OPENCITATIONS_ACCESS_TOKEN"] == "from-user-file"
