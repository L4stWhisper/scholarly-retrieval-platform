from scholarly_retrieval.normalization import (
    normalize_acl_anthology_id,
    normalize_arxiv_id,
    normalize_doi,
    normalize_openalex_id,
    normalize_openreview_id,
)


def test_normalize_doi_urls_and_case() -> None:
    assert normalize_doi("https://doi.org/10.1145/ABC.123") == "10.1145/abc.123"
    assert normalize_doi("DOI: 10.1000/Test.") == "10.1000/test"
    assert normalize_doi("not a doi") is None


def test_normalize_openalex_and_arxiv_ids() -> None:
    assert normalize_openalex_id("https://openalex.org/w123") == "W123"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2401.12345v2.pdf") == "2401.12345v2"
    assert normalize_arxiv_id("arXiv:2401.12345v2", keep_version=False) == "2401.12345"


def test_normalize_openreview_and_acl_anthology_ids() -> None:
    assert normalize_openreview_id("https://openreview.net/forum?id=abc_DEF-123") == "abc_DEF-123"
    assert normalize_openreview_id("openreview:abc_DEF-123") == "abc_DEF-123"
    assert normalize_acl_anthology_id("https://aclanthology.org/N19-1423.pdf") == "N19-1423"
    assert normalize_acl_anthology_id("10.18653/v1/2024.acl-long.1") == "2024.acl-long.1"
    assert normalize_acl_anthology_id("not an ACL id") is None
