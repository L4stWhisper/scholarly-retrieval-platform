import pytest

from tools.diagnose_scholar_pagination import official_next


def test_official_next_preserves_query_without_credentials():
    params = official_next(
        "https://serpapi.com/search.json?engine=google_scholar&cites=111&start=10"
        "&lr=lang_en&scipsc=1&api_key=secret&TOKEN=secret&no_cache=false",
        "111",
    )
    assert params["lr"] == "lang_en"
    assert params["scipsc"] == "1"
    assert params["no_cache"] == "true"
    assert "secret" not in str(params)


@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/search.json?engine=google_scholar&cites=111",
        "https://serpapi.com/search.json?engine=google_scholar&cites=222",
        "https://serpapi.com/search.json?engine=google&cites=111",
    ],
)
def test_official_next_rejects_changed_destination_or_seed(link):
    with pytest.raises(ValueError):
        official_next(link, "111")
