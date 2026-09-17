"""Environment loading and secret-safe configuration diagnostics."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment(path: str | Path | None = None) -> Path | None:
    """Load one project .env without overriding deployment environment values.

    The current working directory is intentional: CLI/API/MCP normally start
    from a project or deployment directory. SCHOLAR_ENV_FILE provides an
    explicit path for services launched elsewhere.
    """

    configured = path or os.getenv("SCHOLAR_ENV_FILE")
    env_path = Path(configured).expanduser() if configured else Path.cwd() / ".env"
    if not env_path.is_file():
        return None
    load_dotenv(dotenv_path=env_path, override=False)
    return env_path.resolve()


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
