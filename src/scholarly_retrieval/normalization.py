"""Identifier normalization with original provider values retained in claims."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)
OPENALEX_RE = re.compile(r"^(?:https?://openalex\.org/)?(W\d+)$", re.IGNORECASE)
ARXIV_RE = re.compile(
    r"^(?:arxiv:|https?://arxiv\.org/(?:abs|pdf)/)?"
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))"
    r"(?P<version>v\d+)?(?:\.pdf)?$",
    re.IGNORECASE,
)
OPENREVIEW_ID_RE = re.compile(r"^[A-Za-z0-9_~-]{3,128}$")
ACL_ANTHOLOGY_ID_RE = re.compile(
    r"^(?:[A-Za-z]\d{2}-\d{4}|\d{4}\.[A-Za-z0-9-]+\.\d+)$",
    re.IGNORECASE,
)


def normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalize_doi(value: str) -> str | None:
    candidate = unquote(value.strip())
    candidate = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", candidate, flags=re.I)
    candidate = re.sub(r"^doi:\s*", "", candidate, flags=re.I)
    candidate = candidate.rstrip(".,;)").lower()
    return candidate if DOI_RE.fullmatch(candidate) else None


def normalize_openalex_id(value: str) -> str | None:
    match = OPENALEX_RE.fullmatch(value.strip())
    return match.group(1).upper() if match else None


def normalize_arxiv_id(value: str, *, keep_version: bool = True) -> str | None:
    match = ARXIV_RE.fullmatch(value.strip())
    if not match:
        return None
    version = match.group("version") or ""
    return match.group("id").lower() + (version.lower() if keep_version else "")


def normalize_openreview_id(value: str) -> str | None:
    """Normalize an OpenReview note/forum ID or public forum URL."""

    candidate = unquote(value.strip())
    url_match = re.match(
        r"^https?://(?:www\.)?openreview\.net/(?:forum|pdf)\?id=([^&#]+)",
        candidate,
        re.I,
    )
    if url_match:
        candidate = unquote(url_match.group(1))
    candidate = re.sub(r"^openreview:\s*", "", candidate, flags=re.I)
    return candidate if OPENREVIEW_ID_RE.fullmatch(candidate) else None


def normalize_acl_anthology_id(value: str) -> str | None:
    """Normalize an Anthology ID, Anthology URL, or canonical 10.18653 DOI."""

    candidate = unquote(value.strip())
    doi = normalize_doi(candidate)
    if doi and doi.startswith("10.18653/v1/"):
        candidate = doi[len("10.18653/v1/") :]
    else:
        url_match = re.match(
            r"^https?://(?:www\.)?aclanthology\.org/([^/?#]+?)(?:\.(?:pdf|xml|bib))?/?(?:[?#].*)?$",
            candidate,
            re.I,
        )
        if url_match:
            candidate = url_match.group(1)
        candidate = re.sub(r"^(?:acl|acl_anthology):\s*", "", candidate, flags=re.I)
    if not ACL_ANTHOLOGY_ID_RE.fullmatch(candidate):
        return None
    # ACL's historical IDs use an uppercase collection letter, while the
    # post-2020 dotted venue form is canonically lowercase after the year.
    if re.fullmatch(r"[A-Za-z]\d{2}-\d{4}", candidate):
        return candidate[0].upper() + candidate[1:]
    year, remainder = candidate.split(".", 1)
    return f"{year}.{remainder.casefold()}"
