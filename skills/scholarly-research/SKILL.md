---
name: scholarly-research
description: Use the scholarly-retrieval MCP or scholar CLI to find academic papers by keywords or filters, resolve identifiers, trace references and citations, discover related work, and report source-aware results.
---

# Scholarly Research

Use this project's installed tools for literature discovery. Prefer the `scholarly-retrieval` MCP
because it returns structured data directly. Use the `scholar` CLI for terminal workflows or when
MCP is unavailable. Both call the same Python library; do not call both merely to duplicate a
request.

Let the platform perform provider routing, retries, aggregation, deduplication, citation-edge
normalization, caching, and persistence. Do not replace these functions with ad-hoc scraping or
model-side title deduplication.

## Choose the operation

| Intent | MCP tool | CLI command | Initial budget |
|---|---|---|---|
| Inspect source capabilities | `providers` | `scholar providers` | n/a |
| Keyword or advanced search | `search` | `scholar search` | 5-10 papers |
| Resolve a DOI or other ID | `resolve` | `scholar resolve` | 1 seed |
| Papers cited by a seed | `references` | `scholar references` | 10-20 papers |
| Papers citing a seed | `citations` | `scholar citations` | 10-20 papers |
| Related-paper discovery | `related` | `scholar related` | 10-15 papers |
| Bounded citation graph | `expand` | `scholar graph expand` | depth 1 |
| Parse authorized local text/PDF | `extract_references` | `scholar extract-references` | 1 document |
| Resolve parsed bibliography | `link_references` | `scholar link-references` | parsed references |

Use advanced `search` fields for author, year, venue, field, work type, open access, minimum
citations, and sorting. Do not hide all filters inside the keyword string. Resolve a known DOI,
arXiv ID, PMID/PMCID, OpenAlex ID, Semantic Scholar ID, OpenReview ID, or ACL Anthology ID before
following relationships when its identity is uncertain.

Use the MCP schema presented by the client as the authority for MCP argument names. Use
`scholar COMMAND --help` as the authority for CLI options.

## Select providers only when useful

The open registry contains `openalex`, `openaire`, `semantic_scholar`, `crossref`, `datacite`,
`dblp`, `acl_anthology`, `arxiv`, `openreview`, `europe_pmc`, `inspire`, and `opencitations`.
`google_scholar_serpapi` exists only when the user supplies its key.

Normally let capability routing choose sources. Select sources when the user requests them, a
domain source is useful, or comparing coverage is part of the task. Useful combinations include:

- NLP: `acl_anthology,dblp,arxiv,openalex`;
- ML conferences: `openreview,openalex,dblp`;
- biomedicine: `europe_pmc,openalex,crossref`;
- high-energy physics: `inspire,openalex,arxiv`;
- DOI/citation evidence: `openalex,semantic_scholar,opencitations,crossref`.

Never force a provider into an operation it does not support. A citation count is not a
traversable citation list.

## Run a bounded workflow

Start with small limits. Review relevance and `provider_reports`, then increase only if the task
needs more coverage. For a literature review:

1. search for 5-10 candidates;
2. choose and resolve a small number of seed papers;
3. follow `references`, `citations`, or `related` according to the question;
4. expand a graph only with explicit `depth`, per-node, frontier, and Top-K budgets;
5. summarize selected evidence and disclose partial coverage.

Each full paper record can be large. Return compact fields such as title, year, authors,
identifiers, URL, source provenance, and why it was selected unless the user requests complete raw
records. Stored SQLite history and provider raw responses do not enter model context unless a tool
explicitly returns them.

## Inspect evidence before answering

Check `status`, `provider_reports`, `truncated`, cursors, unresolved references, and filter
execution. Treat `partial`, `throttled`, `skipped`, `failed`, and `empty` as distinct states; never
describe a partial result as complete coverage.

For deeper diagnosis inspect:

- `source_records` and `field_claims` for source-specific values;
- `identity_decisions` for merge, conflict, or review evidence;
- `assertions` and `edges` for citation evidence and verification state;
- `discovery_paths`, `filter_exclusions`, and `truncation_reasons` for graph selection;
- `rankings[].evidence` for related-paper method, seed context, and RRF contribution;
- `provider_contributions` and `provider_overlaps` for bounded-result source gain, not database
  recall;
- `fingerprint` for stable replay identity excluding retrieval timestamps.

Citation direction is always `citing -> cited`: `references(seed)` produces `seed -> referenced
work`; `citations(seed)` produces `citing work -> seed`. Never add citation counts from providers.
Do not silently merge title-only matches, turn ambiguous reference candidates into edges, or call
an RRF score a calibrated relevance probability.

## CLI fallback and environment rules

Run CLI commands from the project root so the intended `.env` is discovered:

```bash
cd /d/github_project/scholarly_retrieval_platform
scholar search "citation graph" --limit 5
# Equivalent module fallback:
python -m scholarly_retrieval search "citation graph" --limit 5
```

The module name is `scholarly_retrieval`, not `scholar`. In Git Bash use `/c/...` or `/d/...`
paths rather than unescaped Windows backslashes. If the installed environment is not on PATH, use
that environment's absolute Python path in the MCP configuration.

If a source requires credentials, institutional access, browser access, or licensed full text,
report the dependency and continue with authorized sources. Never bypass a CAPTCHA, paywall,
access control, robots policy, or redistribution rule. The project returns PDF URLs but does not
download papers automatically; only process local documents the user is authorized to use.
