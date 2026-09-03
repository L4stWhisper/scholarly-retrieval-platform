"""Repository contracts that keep the three project documents and Agent entry point aligned."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_DOCUMENTS = [
    ROOT / "README.md",
    ROOT / "docs" / "technical-implementation.md",
    ROOT / "docs" / "test-acceptance.md",
]
LINK_CHECK_DOCUMENTS = [
    *MAIN_DOCUMENTS,
    ROOT / "skills" / "scholarly-research" / "SKILL.md",
]


def test_public_entry_points_and_versions_stay_aligned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.1.0"
    assert project["scripts"] == {
        "scholar": "scholarly_retrieval.cli:app",
        "scholar-api": "scholarly_retrieval.api:main",
        "scholar-mcp": "scholarly_retrieval.mcp_server:main",
        "scholar-worker": "scholarly_retrieval.worker:main",
    }
    package = (ROOT / "src" / "scholarly_retrieval" / "__init__.py").read_text(
        encoding="utf-8"
    )
    api = (ROOT / "src" / "scholarly_retrieval" / "api.py").read_text(encoding="utf-8")
    assert '__version__ = "0.1.0"' in package
    assert 'version="0.1.0"' in api


def test_repository_has_exactly_three_human_facing_project_documents() -> None:
    assert all(document.is_file() for document in MAIN_DOCUMENTS)
    assert sorted((ROOT / "docs").rglob("*.md")) == sorted(MAIN_DOCUMENTS[1:])

    # SKILL.md is executable Agent instruction/configuration, not a fourth project manual.
    skills = list((ROOT / "skills").rglob("SKILL.md"))
    assert skills == [ROOT / "skills" / "scholarly-research" / "SKILL.md"]
    assert not (ROOT / "skills" / "scholarly-research" / "references").exists()

    removed_root_guides = {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "scholarly-retrieval-platform-design.md",
    }
    assert not removed_root_guides.intersection(path.name for path in ROOT.glob("*.md"))


def test_user_documentation_has_no_broken_local_links() -> None:
    markdown_link = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    broken: list[str] = []
    for document in LINK_CHECK_DOCUMENTS:
        assert document.is_file(), f"missing required documentation: {document.relative_to(ROOT)}"
        for target in markdown_link.findall(document.read_text(encoding="utf-8")):
            path_text = target.split("#", 1)[0].strip()
            if not path_text or "://" in path_text or path_text.startswith("mailto:"):
                continue
            resolved = (document.parent / path_text).resolve()
            if not resolved.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_readme_is_the_complete_user_manual() -> None:
    readme = MAIN_DOCUMENTS[0].read_text(encoding="utf-8")
    for retrieval_mode in ("关键词检索", "高级检索", "引用关系检索", "相关论文检索"):
        assert retrieval_mode in readme
    for entry_point in ("Python Library", "CLI", "HTTP API", "MCP", "Agent Skill"):
        assert entry_point in readme
    for command in (
        "scholar search",
        "scholar references",
        "scholar citations",
        "scholar related",
        "claude mcp add",
        "codex mcp add",
    ):
        assert command in readme
    assert ".claude\\skills" in readme
    assert ".agents\\skills" in readme


def test_cli_api_and_mcp_running_models_are_documented_separately() -> None:
    readme = MAIN_DOCUMENTS[0].read_text(encoding="utf-8")
    assert "CLI 是一次命令、一次结果，不监听端口" in readme
    assert "scholar-api" in readme and "127.0.0.1:8000" in readme
    assert "Agent 客户端负责启动 `scholar-mcp`" in readme
    assert "SCHOLAR_MCP_TRANSPORT" in readme and "127.0.0.1:8001/mcp" in readme


def test_secret_file_is_ignored_but_template_is_publishable() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert ".env" in patterns
    assert ".env.example" not in patterns
    assert (ROOT / ".env.example").is_file()
    assert ".env" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "API_KEY" not in (ROOT / ".mcp.json").read_text(encoding="utf-8")


def test_scholarly_research_skill_is_self_contained_and_tool_focused() -> None:
    skill = LINK_CHECK_DOCUMENTS[-1].read_text(encoding="utf-8")
    for unrelated_context in ("autoresearch", "Recursive Self-Improvement", "RSI"):
        assert unrelated_context not in skill
    for operation in (
        "providers",
        "search",
        "resolve",
        "references",
        "citations",
        "related",
        "expand",
        "extract_references",
        "link_references",
    ):
        assert f"`{operation}`" in skill
    assert "Prefer the `scholarly-retrieval` MCP" in skill
    assert "scholar COMMAND --help" in skill
    assert "references/" not in skill


def test_technical_and_acceptance_docs_cover_traceability_and_storage() -> None:
    technical = MAIN_DOCUMENTS[1].read_text(encoding="utf-8")
    acceptance = MAIN_DOCUMENTS[2].read_text(encoding="utf-8")
    for term in (
        "SCHOLAR_DB_PATH",
        "query_runs",
        "raw_responses",
        "field_claims",
        "citation_assertions",
        "visible_citation_edges",
        "scholar maintenance",
        "ADR-0022",
    ):
        assert term in technical
    for term in ("ruff", "compileall", "pytest", "Claude Code", "C01", "C10"):
        assert term in acceptance
