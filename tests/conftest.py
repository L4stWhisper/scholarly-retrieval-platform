from __future__ import annotations

from typing import Any


def make_openalex_work(
    work_id: str,
    *,
    title: str = "A Test Paper",
    doi: str | None = "https://doi.org/10.1234/example",
    references: list[str] | None = None,
    cited_by_count: int = 3,
) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{work_id}",
        "doi": doi,
        "title": title,
        "display_name": title,
        "abstract_inverted_index": {"A": [0], "test": [1], "abstract": [2]},
        "authorships": [
            {
                "author": {
                    "id": "https://openalex.org/A123",
                    "display_name": "Ada Researcher",
                    "orcid": "https://orcid.org/0000-0000-0000-0001",
                }
            }
        ],
        "publication_date": "2024-03-01",
        "publication_year": 2024,
        "type": "article",
        "language": "en",
        "primary_location": {
            "landing_page_url": "https://example.org/paper",
            "pdf_url": "https://example.org/paper.pdf",
            "source": {"display_name": "Journal of Tests"},
        },
        "open_access": {"is_oa": True},
        "ids": {"openalex": f"https://openalex.org/{work_id}", "doi": doi},
        "cited_by_count": cited_by_count,
        "referenced_works": references or [],
    }
