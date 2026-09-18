"""Repository contracts keeping the README, docs/ guides, and Agent entry point aligned."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
DOCS = ROOT / "docs"
ENGINEERING_DOCUMENTS = [
    DOCS / "technical-implementation.md",
    DOCS / "test-acceptance.md",
]
USER_GUIDES = [
    DOCS / name
    for name in (
        "installation.md",
        "configuration.md",
        "cli.md",
        "library.md",
        "api.md",
        "mcp.md",
        "providers.md",
        "references-and-pdf.md",
        "storage-and-deployment.md",
        "faq.md",
        "development.md",
    )
]
SKILL = ROOT / "skills" / "scholarly-research" / "SKILL.md"
LINK_CHECK_DOCUMENTS = [README, *sorted(DOCS.glob("*.md")), SKILL]


def _guides_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in USER_GUIDES)


def test_public_entry_points_and_versions_stay_aligned() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.1.0"
    assert project["scripts"] == {
        "scholar": "scholarly_retrieval.cli:app",
        "scholar-api": "scholarly_retrieval.api:main",
        "scholar-mcp": "scholarly_retrieval.mcp_server:main",
        "scholar-worker": "scholarly_retrieval.worker:main",
    }
    package = (ROOT / "src" / "scholarly_retrieval" / "__init__.py").read_text(encoding="utf-8")
    api = (ROOT / "src" / "scholarly_retrieval" / "api.py").read_text(encoding="utf-8")
    assert '__version__ = "0.1.0"' in package
    assert 'version="0.1.0"' in api


def test_repository_keeps_readme_guides_and_engineering_documents() -> None:
    assert README.is_file()
    assert all(document.is_file() for document in ENGINEERING_DOCUMENTS)
    assert all(guide.is_file() for guide in USER_GUIDES)

    # SKILL.md is executable Agent instruction/configuration, not another manual.
    skills = list((ROOT / "skills").rglob("SKILL.md"))
    assert skills == [SKILL]
    assert not (ROOT / "skills" / "scholarly-research" / "references").exists()

    # Root-level guides were folded into docs/; keep the root focused on README.
    removed_root_guides = {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "scholarly-retrieval-platform-design.md",
    }
    assert not removed_root_guides.intersection(path.name for path in ROOT.glob("*.md"))


def test_user_documentation_has_no_broken_local_links() -> None:
    markdown_link = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
    html_link = re.compile(r'href="([^"]+)"')
    broken: list[str] = []
    for document in LINK_CHECK_DOCUMENTS:
        assert document.is_file(), f"missing required documentation: {document.relative_to(ROOT)}"
        text = document.read_text(encoding="utf-8")
        for target in [*markdown_link.findall(text), *html_link.findall(text)]:
            path_text = target.split("#", 1)[0].strip()
            if not path_text or "://" in path_text or path_text.startswith("mailto:"):
                continue
            resolved = (document.parent / path_text).resolve()
            if not resolved.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")
    assert broken == []


def test_readme_is_a_short_entry_point_that_links_every_guide() -> None:
    readme = README.read_text(encoding="utf-8")
    # Title, one-line pitch, badges, features, get started, docs index: nothing else.
    for section in ("## 功能", "## 快速开始", "## 文档", "## 许可证"):
        assert section in readme
    assert len(readme.splitlines()) < 200
    for guide in [*USER_GUIDES, *ENGINEERING_DOCUMENTS]:
        assert f"docs/{guide.name}" in readme, f"README must link {guide.name}"
    for command in ("uv sync", "scholar search", "scholar citations", "claude mcp add"):
        assert command in readme
    for retrieval_mode in ("关键词检索", "高级检索", "引用关系检索", "相关论文检索"):
        assert retrieval_mode in readme


def test_guides_form_the_complete_user_manual() -> None:
    guides = _guides_text()
    for retrieval_mode in ("关键词检索", "高级检索", "引用关系检索", "相关论文检索"):
        assert retrieval_mode in guides
    for entry_point in ("Python Library", "CLI", "HTTP API", "MCP", "Agent Skill"):
        assert entry_point in guides
    for command in (
        "scholar search",
        "scholar references",
        "scholar citations",
        "scholar related",
        "scholar doctor",
        "claude mcp add",
        "codex mcp add",
    ):
        assert command in guides
    assert ".claude\\skills" in guides
    assert ".agents\\skills" in guides
    # Environment precedence and the user-level file are user-facing contracts.
    assert "~/.config/scholarly-retrieval/.env" in guides
    assert "SCHOLAR_ENV_FILE" in guides


def test_cli_api_and_mcp_running_models_are_documented_separately() -> None:
    guides = _guides_text()
    assert "CLI 是一次命令、一次结果，不监听端口" in guides
    assert "scholar-api" in guides and "127.0.0.1:8000" in guides
    assert "Agent 客户端负责启动 `scholar-mcp`" in guides
    assert "SCHOLAR_MCP_TRANSPORT" in guides and "127.0.0.1:8001/mcp" in guides


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
    skill = SKILL.read_text(encoding="utf-8")
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
    technical = ENGINEERING_DOCUMENTS[0].read_text(encoding="utf-8")
    acceptance = ENGINEERING_DOCUMENTS[1].read_text(encoding="utf-8")
    for term in (
        "SCHOLAR_DB_PATH",
        "query_runs",
        "raw_responses",
        "field_claims",
        "citation_assertions",
        "visible_citation_edges",
        "scholar maintenance",
        "ADR-0022",
        "ADR-0023",
    ):
        assert term in technical
    for term in ("ruff", "compileall", "pytest", "Claude Code", "C01", "C10"):
        assert term in acceptance
