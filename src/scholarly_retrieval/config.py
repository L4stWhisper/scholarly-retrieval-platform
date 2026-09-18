"""Environment loading and secret-safe configuration diagnostics."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

USER_ENV_RELATIVE_PATH = Path(".config") / "scholarly-retrieval" / ".env"


def environment_file_candidates(path: str | Path | None = None) -> list[Path]:
    """Search order for the optional .env file; the first existing file wins.

    1. an explicit ``path`` argument or ``SCHOLAR_ENV_FILE``;
    2. ``.env`` in the current working directory (a project checkout);
    3. ``~/.config/scholarly-retrieval/.env`` (one user-level file shared by
       every checkout and by Agent clients launched from other directories).
    """

    configured = path or os.getenv("SCHOLAR_ENV_FILE")
    if configured:
        return [Path(configured).expanduser()]
    return [Path.cwd() / ".env", Path.home() / USER_ENV_RELATIVE_PATH]


def load_environment(path: str | Path | None = None) -> Path | None:
    """Load at most one .env file without overriding process environment values.

    Variables already present in the process environment always take
    precedence, so shell exports, CI secrets, and container settings are never
    overwritten by a file. The file is a convenience for local use only.
    """

    for env_path in environment_file_candidates(path):
        if env_path.is_file():
            load_dotenv(dotenv_path=env_path, override=False)
            return env_path.resolve()
    return None


def credential_status() -> dict[str, dict[str, bool]]:
    """Report presence only; secret values must never enter logs or CLI output."""

    return {
        "ads": {"api_token_configured": bool(os.getenv("ADS_API_TOKEN"))},
        "openalex": {
            "api_key_configured": bool(os.getenv("OPENALEX_API_KEY")),
            "contact_configured": bool(os.getenv("OPENALEX_MAILTO")),
        },
        "semantic_scholar": {
            "api_key_configured": bool(os.getenv("SEMANTIC_SCHOLAR_API_KEY")),
        },
        "google_scholar_serpapi": {
            "api_key_configured": bool(os.getenv("SERPAPI_API_KEY")),
        },
        "crossref": {
            "contact_configured": bool(os.getenv("CROSSREF_MAILTO")),
        },
        "arxiv": {
            "contact_configured": bool(os.getenv("ARXIV_MAILTO")),
        },
        "europe_pmc": {
            "contact_configured": bool(os.getenv("EUROPE_PMC_EMAIL")),
        },
        "opencitations": {
            "access_token_configured": bool(os.getenv("OPENCITATIONS_ACCESS_TOKEN")),
        },
        "distributed": {
            "redis_configured": bool(os.getenv("SCHOLAR_REDIS_URL")),
        },
    }
