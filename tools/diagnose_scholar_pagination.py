"""Bounded CLI comparison of official next URLs and provider reconstruction.

Opt-in live requests consume SerpApi quota. Output contains search IDs and paper
IDs, never credentials; no Claude/MCP or local HTTP cache is involved.
"""

import argparse
import asyncio
import json
import os
from urllib.parse import parse_qsl, urlsplit

import httpx

from scholarly_retrieval.config import load_environment
from scholarly_retrieval.providers.serpapi_google_scholar import SerpApiGoogleScholarProvider
from scholarly_retrieval.reliability import SENSITIVE_QUERY_NAMES


def official_next(link: str, cites: str) -> dict[str, str]:
    url = urlsplit(link)
    if url.scheme != "https" or url.netloc != "serpapi.com" or url.path != "/search.json":
        raise ValueError("unexpected pagination endpoint")
    params = dict(parse_qsl(url.query))
    if params.get("engine") != "google_scholar" or params.get("cites") != cites:
        raise ValueError("changed query")
    # Preserve the returned query, changing only authentication/cache controls.
    params = {k: v for k, v in params.items() if k.casefold() not in SENSITIVE_QUERY_NAMES}
    params["no_cache"] = "true"
    return params


async def run(args):
    load_environment()
    key = os.environ["SERPAPI_API_KEY"]
    async with httpx.AsyncClient(timeout=45) as client:
        for repetition in range(args.rounds):
            modes = ["official", "project"] if repetition % 2 == 0 else ["project", "official"]
            if args.mode != "both":
                modes = [args.mode]
            for mode in modes:
                params = {
                    "engine": "google_scholar",
                    "cites": args.cites,
                    "num": "10",
                    "start": "0",
                    "hl": "zh-CN",
                    "as_sdt": "2005",
                    "filter": "0",
                    "no_cache": "true",
                }
                if args.filter == "default":
                    params.pop("filter")
                else:
                    params["filter"] = args.filter
                unique = set()
                end = "page_budget"
                seen = set()
                for _ in range(args.max_pages):
                    marker = tuple(sorted(params.items()))
                    if marker in seen:
                        end = "loop"
                        break
                    seen.add(marker)
                    try:
                        response = await client.get(
                            "https://serpapi.com/search.json", params={**params, "api_key": key}
                        )
                        response.raise_for_status()
                        payload = response.json()
                    except (httpx.HTTPError, ValueError) as exc:
                        print(json.dumps({"mode": mode, "error": type(exc).__name__}), flush=True)
                        end = "request_error"
                        break
                    items = payload.get("organic_results", [])
                    ids = [
                        str(i.get("result_id") or i.get("link") or i.get("title")) for i in items
                    ]
                    unique.update(ids)
                    link = payload.get("serpapi_pagination", {}).get("next")
                    direct = official_next(link, args.cites) if link else None
                    reconstructed = SerpApiGoogleScholarProvider._next_params(payload, params)
                    if reconstructed is not None:
                        reconstructed["engine"] = "google_scholar"
                    delta = {
                        k: {
                            "official": (direct or {}).get(k),
                            "project": (reconstructed or {}).get(k),
                        }
                        for k in set(direct or {}) | set(reconstructed or {})
                        if (direct or {}).get(k) != (reconstructed or {}).get(k)
                    }
                    print(
                        json.dumps(
                            {
                                "round": repetition + 1,
                                "mode": mode,
                                "request": params,
                                "search_id": payload.get("search_metadata", {}).get("id"),
                                "returned": len(items),
                                "ids": ids,
                                "total": payload.get("search_information", {}).get("total_results"),
                                "has_next": bool(link),
                                "next_parameter_delta": delta,
                                "has_api_error": bool(payload.get("error")),
                            }
                        ),
                        flush=True,
                    )
                    if not link:
                        end = "no_next"
                        break
                    params = direct if mode == "official" else reconstructed
                print(
                    json.dumps(
                        {
                            "summary": True,
                            "round": repetition + 1,
                            "mode": mode,
                            "unique": len(unique),
                            "end": end,
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cites", required=True)
    parser.add_argument("--rounds", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--max-pages", type=int, choices=range(1, 11), default=5)
    parser.add_argument(
        "--live", action="store_true", help="authorize quota-consuming API requests"
    )
    parser.add_argument("--filter", choices=["0", "1", "default"], default="0")
    parser.add_argument("--mode", choices=["official", "project", "both"], default="both")
    options = parser.parse_args()
    if not options.live or not options.cites.isdigit():
        parser.error("--live and a numeric --cites ID are required")
    asyncio.run(run(options))
