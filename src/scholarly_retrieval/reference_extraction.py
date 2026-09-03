"""Reference extraction from legally supplied structured full text."""

from __future__ import annotations

import asyncio
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from .models import (
    CitationContext,
    ExtractedReference,
    ReferenceEvidenceLevel,
    ReferenceExtractionResult,
)
from .normalization import normalize_doi

XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[^\s<>\]}]+", re.IGNORECASE)
MAX_PDF_BYTES = 100 * 1024 * 1024
_BIBTEX_SKIPPED_TYPES = {"comment", "preamble", "string"}
MAX_CONTEXT_CHARS = 1000


def extract_references_from_text(
    content: str,
    *,
    source_format: str,
    include_uncited: bool = False,
) -> ReferenceExtractionResult:
    """Dispatch safe text formats for Library, CLI, HTTP, and MCP callers."""

    normalized_format = source_format.strip().casefold()
    if normalized_format == "bibtex":
        return extract_bibtex_references(content)
    extractors = {
        "jats": extract_jats_references,
        "tei": extract_tei_references,
        "latex": extract_latex_references,
    }
    extractor = extractors.get(normalized_format)
    if extractor is None:
        raise ValueError("text reference format must be jats, tei, latex, or bibtex")
    return extractor(content, include_uncited=include_uncited)


def extract_jats_references(
    xml_text: str,
    *,
    include_uncited: bool = False,
) -> ReferenceExtractionResult:
    """Extract JATS bibliography entries and verify them against body callouts."""

    root = _safe_xml_root(xml_text, "JATS", allow_external_doctype=True)

    callouts, contexts = _xml_callouts_and_contexts(
        root,
        anchor_name="xref",
        target_attribute="rid",
        anchor_predicate=lambda element: element.attrib.get("ref-type") == "bibr",
        normalize_target=lambda target: target,
    )

    references: list[ExtractedReference] = []
    uncited_ids: list[str] = []
    bibliography_count = 0
    for element in root.iter():
        if _local_name(element.tag) != "ref":
            continue
        bibliography_count += 1
        reference_id = element.attrib.get("id") or f"ref-{bibliography_count}"
        cited = callouts[reference_id] > 0
        if not cited:
            uncited_ids.append(reference_id)
            if not include_uncited:
                continue
        raw_text = _normalized_text(element)
        year_text = _first_text(element, "year")
        year_match = re.search(r"\b(1\d{3}|2\d{3})\b", year_text or "")
        doi_text = _first_pub_id(element, "doi")
        references.append(
            ExtractedReference(
                reference_id=reference_id,
                ordinal=bibliography_count,
                cited_in_text=cited,
                evidence_level=(
                    ReferenceEvidenceLevel.VERIFIED_ANCHOR
                    if cited
                    else ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY
                ),
                raw_text=raw_text,
                title=_first_text(element, "article-title"),
                authors=_authors(element),
                publication_year=int(year_match.group(1)) if year_match else None,
                doi=normalize_doi(doi_text) if doi_text else None,
                venue=_first_text(element, "source"),
                volume=_first_text(element, "volume"),
                issue=_first_text(element, "issue"),
                pages=_jats_pages(element),
                callout_count=callouts[reference_id],
                contexts=contexts.get(reference_id, []),
            )
        )

    unresolved_targets = sorted(
        set(callouts)
        - {element.attrib.get("id") for element in root.iter() if _local_name(element.tag) == "ref"}
    )
    warnings = [
        f"body callout target not found in bibliography: {target}" for target in unresolved_targets
    ]
    return ReferenceExtractionResult(
        source_format="jats",
        references=references,
        bibliography_entry_count=bibliography_count,
        cited_entry_count=sum(1 for target in callouts if target not in unresolved_targets),
        uncited_reference_ids=uncited_ids,
        warnings=warnings,
    )


def extract_tei_references(
    xml_text: str,
    *,
    include_uncited: bool = False,
) -> ReferenceExtractionResult:
    """Extract TEI/GROBID bibliography entries verified by body ``ref`` anchors."""

    root = _safe_xml_root(xml_text, "TEI")
    callouts, contexts = _xml_callouts_and_contexts(
        root,
        anchor_name="ref",
        target_attribute="target",
        anchor_predicate=lambda element: (
            element.attrib.get("type", "").casefold() == "bibr"
            or any(target.startswith("#") for target in element.attrib.get("target", "").split())
        ),
        normalize_target=lambda target: target.lstrip("#"),
    )

    bibliography: list[ElementTree.Element] = []
    for container in root.iter():
        if _local_name(container.tag) != "listBibl":
            continue
        bibliography.extend(
            child
            for child in container.iter()
            if child is not container and _local_name(child.tag) in {"bibl", "biblStruct"}
        )

    references: list[ExtractedReference] = []
    uncited_ids: list[str] = []
    known_ids: set[str] = set()
    for ordinal, element in enumerate(bibliography, start=1):
        reference_id = element.attrib.get(XML_ID) or element.attrib.get("id") or f"ref-{ordinal}"
        known_ids.add(reference_id)
        cited = callouts[reference_id] > 0
        if not cited:
            uncited_ids.append(reference_id)
            if not include_uncited:
                continue
        date_element = _first_element(element, "date")
        year_source = (date_element.attrib.get("when", "") if date_element is not None else "") or (
            _normalized_text(date_element) if date_element is not None else ""
        )
        year_match = re.search(r"\b(1\d{3}|2\d{3})\b", year_source)
        doi_text = _first_typed_text(element, "idno", "type", "doi")
        title = _tei_title(element)
        references.append(
            ExtractedReference(
                reference_id=reference_id,
                ordinal=ordinal,
                cited_in_text=cited,
                evidence_level=(
                    ReferenceEvidenceLevel.VERIFIED_ANCHOR
                    if cited
                    else ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY
                ),
                raw_text=_normalized_text(element),
                title=title,
                authors=_tei_authors(element),
                publication_year=int(year_match.group(1)) if year_match else None,
                doi=normalize_doi(doi_text) if doi_text else None,
                venue=_tei_venue(element),
                volume=_tei_bibl_scope(element, {"volume", "vol"}),
                issue=_tei_bibl_scope(element, {"issue", "number"}),
                pages=_tei_bibl_scope(element, {"page", "pages", "pp"}),
                callout_count=callouts[reference_id],
                contexts=contexts.get(reference_id, []),
            )
        )
    unresolved = sorted(set(callouts) - known_ids)
    return ReferenceExtractionResult(
        source_format="tei",
        references=references,
        bibliography_entry_count=len(bibliography),
        cited_entry_count=sum(1 for target in callouts if target in known_ids),
        uncited_reference_ids=uncited_ids,
        warnings=[
            f"body callout target not found in bibliography: {target}" for target in unresolved
        ],
    )


def extract_latex_references(
    latex_text: str,
    *,
    include_uncited: bool = False,
) -> ReferenceExtractionResult:
    """Link LaTeX cite keys to an embedded ``thebibliography`` environment."""

    callouts: Counter[str] = Counter()
    contexts: dict[str, list[CitationContext]] = defaultdict(list)
    cite_pattern = re.compile(r"\\cite\w*\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}", re.DOTALL)
    latex_without_comments = _strip_latex_comments(latex_text)
    for match in cite_pattern.finditer(latex_without_comments):
        for key in match.group(1).split(","):
            normalized_key = key.strip()
            if normalized_key:
                callouts[normalized_key] += 1
                contexts[normalized_key].append(
                    CitationContext(
                        context_id=f"{normalized_key}:context-{callouts[normalized_key]}",
                        text=_latex_context(latex_without_comments, match.start(), match.end()),
                        anchor_text=match.group(0),
                        source_locator=f"character:{match.start()}",
                    )
                )

    item_pattern = re.compile(r"\\bibitem(?:\[[^\]]*\])?\{([^}]+)\}")
    items = list(item_pattern.finditer(latex_text))
    references: list[ExtractedReference] = []
    uncited_ids: list[str] = []
    known_ids: set[str] = set()
    for ordinal, match in enumerate(items, start=1):
        key = match.group(1).strip()
        known_ids.add(key)
        end = items[ordinal].start() if ordinal < len(items) else len(latex_text)
        raw = " ".join(latex_text[match.end() : end].split())
        raw = re.split(r"\\end\{thebibliography\}", raw, maxsplit=1)[0].strip()
        cited = callouts[key] > 0
        if not cited:
            uncited_ids.append(key)
            if not include_uncited:
                continue
        year_match = re.search(r"\b(1\d{3}|2\d{3})\b", raw)
        doi_match = DOI_PATTERN.search(raw)
        references.append(
            ExtractedReference(
                reference_id=key,
                ordinal=ordinal,
                cited_in_text=cited,
                evidence_level=(
                    ReferenceEvidenceLevel.VERIFIED_ANCHOR
                    if cited
                    else ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY
                ),
                raw_text=raw,
                publication_year=int(year_match.group(1)) if year_match else None,
                doi=normalize_doi(doi_match.group(0)) if doi_match else None,
                callout_count=callouts[key],
                contexts=contexts.get(key, []),
            )
        )
    unresolved = sorted(set(callouts) - known_ids)
    return ReferenceExtractionResult(
        source_format="latex",
        references=references,
        bibliography_entry_count=len(items),
        cited_entry_count=sum(1 for target in callouts if target in known_ids),
        uncited_reference_ids=uncited_ids,
        warnings=[f"cite key not found in bibliography: {target}" for target in unresolved],
    )


def extract_bibtex_references(bibtex_text: str) -> ReferenceExtractionResult:
    """Extract candidates from an external BibTeX database.

    A standalone ``.bib`` file contains no document-body callouts. Consequently,
    every returned record deliberately remains ``bibliography_only`` and must be
    linked or corroborated before it can become a verified citation edge.
    """

    references: list[ExtractedReference] = []
    warnings: list[str] = []
    seen_keys: set[str] = set()
    entry_count = 0
    for entry_type, body, raw_entry in _bibtex_entries(bibtex_text):
        if entry_type in _BIBTEX_SKIPPED_TYPES:
            continue
        entry_count += 1
        key, separator, fields_text = body.partition(",")
        key = key.strip()
        if not separator or not key:
            warnings.append(f"BibTeX entry {entry_count} has no citation key or fields")
            continue
        if key in seen_keys:
            warnings.append(f"duplicate BibTeX citation key skipped: {key}")
            continue
        seen_keys.add(key)

        fields, field_warnings = _parse_bibtex_fields(fields_text)
        warnings.extend(f"BibTeX entry {key}: {warning}" for warning in field_warnings)
        year_match = re.search(r"\b(1\d{3}|2\d{3})\b", fields.get("year", ""))
        doi = _bibtex_doi(fields, raw_entry)
        authors = [
            _clean_bibtex_value(author)
            for author in re.split(r"\s+and\s+", fields.get("author", ""), flags=re.IGNORECASE)
            if _clean_bibtex_value(author)
        ]
        references.append(
            ExtractedReference(
                reference_id=key,
                ordinal=entry_count,
                cited_in_text=False,
                evidence_level=ReferenceEvidenceLevel.BIBLIOGRAPHY_ONLY,
                raw_text=" ".join(raw_entry.split()),
                title=_optional_clean_bibtex_value(fields.get("title")),
                authors=authors,
                publication_year=int(year_match.group(1)) if year_match else None,
                doi=doi,
                venue=_optional_clean_bibtex_value(
                    fields.get("journal") or fields.get("booktitle")
                ),
                volume=_optional_clean_bibtex_value(fields.get("volume")),
                issue=_optional_clean_bibtex_value(fields.get("number")),
                pages=_optional_clean_bibtex_value(fields.get("pages")),
                callout_count=0,
            )
        )

    return ReferenceExtractionResult(
        source_format="bibtex",
        references=references,
        bibliography_entry_count=entry_count,
        cited_entry_count=0,
        uncited_reference_ids=[reference.reference_id for reference in references],
        warnings=warnings,
    )


async def extract_pdf_references_with_grobid(
    pdf_bytes: bytes,
    *,
    grobid_url: str,
    include_uncited: bool = False,
    client: httpx.AsyncClient | None = None,
) -> ReferenceExtractionResult:
    """Send a user-authorized PDF to GROBID and parse its TEI response."""

    if not pdf_bytes or len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("PDF must be between 1 byte and 100 MiB")
    parsed = urlparse(grobid_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("GROBID URL must be an absolute http(s) URL")
    owns_client = client is None
    active_client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    try:
        response = await active_client.post(
            f"{grobid_url.rstrip('/')}/api/processFulltextDocument",
            files={"input": ("paper.pdf", pdf_bytes, "application/pdf")},
            data={"consolidateCitations": "0", "includeRawCitations": "1"},
        )
        response.raise_for_status()
        return extract_tei_references(response.text, include_uncited=include_uncited).model_copy(
            update={
                "source_format": "pdf-grobid-tei",
                "processing_steps": ["grobid", "tei"],
            }
        )
    finally:
        if owns_client:
            await active_client.aclose()


def ocr_pdf_with_ocrmypdf(
    pdf_bytes: bytes,
    *,
    command: str = "ocrmypdf",
    language: str | None = None,
    timeout_seconds: float = 600.0,
    temporary_parent: str | Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bytes:
    """Create a searchable PDF with a local OCRmyPDF executable.

    The command is passed as an argv list with ``shell=False``. ``--skip-text``
    preserves born-digital pages while OCRing image-only pages, so callers may
    safely use the same explicit option for mixed PDFs.
    """

    if not pdf_bytes or len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("PDF must be between 1 byte and 100 MiB")
    if not command.strip():
        raise ValueError("OCRmyPDF command must not be blank")
    if timeout_seconds <= 0:
        raise ValueError("OCR timeout must be positive")
    if language is not None and not re.fullmatch(r"[A-Za-z0-9_+-]+", language):
        raise ValueError("OCR language must contain only letters, digits, _, +, or -")

    with tempfile.TemporaryDirectory(
        prefix="scholarly-ocr-",
        dir=temporary_parent,
    ) as temporary_directory:
        input_path = Path(temporary_directory) / "input.pdf"
        output_path = Path(temporary_directory) / "output.pdf"
        input_path.write_bytes(pdf_bytes)
        arguments = [
            command,
            "--skip-text",
            "--deskew",
            "--rotate-pages",
            "--output-type",
            "pdf",
        ]
        if language:
            arguments.extend(["--language", language])
        arguments.extend([str(input_path), str(output_path)])
        try:
            completed = runner(
                arguments,
                shell=False,
                check=False,
                capture_output=True,
                timeout=timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ValueError(
                f"OCRmyPDF executable not found: {command}; install OCRmyPDF and Tesseract"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"OCRmyPDF exceeded {timeout_seconds:g} seconds") from exc
        if completed.returncode != 0:
            raise ValueError(f"OCRmyPDF failed with exit code {completed.returncode}")
        if not output_path.is_file():
            raise ValueError("OCRmyPDF completed without producing an output PDF")
        output = output_path.read_bytes()
        if not output or len(output) > MAX_PDF_BYTES:
            raise ValueError("OCR output PDF must be between 1 byte and 100 MiB")
        return output


async def extract_scanned_pdf_references_with_ocr_and_grobid(
    pdf_bytes: bytes,
    *,
    grobid_url: str,
    include_uncited: bool = False,
    ocrmypdf_command: str = "ocrmypdf",
    ocr_language: str | None = None,
    ocr_temporary_parent: str | Path | None = None,
    client: httpx.AsyncClient | None = None,
    ocr_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> ReferenceExtractionResult:
    """OCR a user-authorized PDF locally, then extract references with GROBID."""

    searchable_pdf = await asyncio.to_thread(
        ocr_pdf_with_ocrmypdf,
        pdf_bytes,
        command=ocrmypdf_command,
        language=ocr_language,
        temporary_parent=ocr_temporary_parent,
        runner=ocr_runner,
    )
    result = await extract_pdf_references_with_grobid(
        searchable_pdf,
        grobid_url=grobid_url,
        include_uncited=include_uncited,
        client=client,
    )
    return result.model_copy(
        update={
            "source_format": "ocr-pdf-grobid-tei",
            "processing_steps": ["ocrmypdf", "grobid", "tei"],
        }
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _xml_callouts_and_contexts(
    root: ElementTree.Element,
    *,
    anchor_name: str,
    target_attribute: str,
    anchor_predicate: Callable[[ElementTree.Element], bool],
    normalize_target: Callable[[str], str],
) -> tuple[Counter[str], dict[str, list[CitationContext]]]:
    """Collect anchors and their nearest paragraph while retaining occurrences."""

    parents = {child: parent for parent in root.iter() for child in parent}
    callouts: Counter[str] = Counter()
    contexts: dict[str, list[CitationContext]] = defaultdict(list)
    for anchor in root.iter():
        if _local_name(anchor.tag) != anchor_name or not anchor_predicate(anchor):
            continue
        targets = [
            normalize_target(target)
            for target in anchor.attrib.get(target_attribute, "").split()
        ]
        context_containers = {"p", "caption", "note"}
        container = anchor
        while (
            container in parents
            and _local_name(container.tag) not in context_containers
        ):
            container = parents[container]
        if _local_name(container.tag) not in context_containers:
            # Do not use a whole body/article as "context": it can include the
            # bibliography itself and would turn unrelated metadata into evidence.
            container = anchor
        # Some malformed publisher XML contains an empty xref/ref node.  Keep
        # the occurrence usable without constructing an invalid empty context.
        text = (
            _normalized_text(container)
            or _normalized_text(anchor)
            or "[citation anchor]"
        )
        text = text[:MAX_CONTEXT_CHARS]
        for target in targets:
            if not target:
                continue
            callouts[target] += 1
            contexts[target].append(
                CitationContext(
                    context_id=f"{target}:context-{callouts[target]}",
                    text=text,
                    anchor_text=_normalized_text(anchor) or None,
                    section_title=_xml_section_title(anchor, parents),
                    source_locator=(
                        anchor.attrib.get(XML_ID) or anchor.attrib.get("id") or None
                    ),
                )
            )
    return callouts, dict(contexts)


def _xml_section_title(
    element: ElementTree.Element,
    parents: dict[ElementTree.Element, ElementTree.Element],
) -> str | None:
    current = element
    while current in parents:
        current = parents[current]
        if _local_name(current.tag) not in {"sec", "div"}:
            continue
        for child in current:
            if _local_name(child.tag) in {"title", "head"}:
                return _normalized_text(child) or None
    return None


def _safe_xml_root(
    xml_text: str,
    label: str,
    *,
    allow_external_doctype: bool = False,
) -> ElementTree.Element:
    upper_prefix = xml_text[:4096].upper()
    if "<!ENTITY" in upper_prefix:
        raise ValueError(f"{label} input must not contain DOCTYPE or ENTITY declarations")
    if "<!DOCTYPE" in upper_prefix:
        declaration = re.search(r"<!DOCTYPE\s+[^>]*>", xml_text, flags=re.IGNORECASE)
        # Europe PMC JATS commonly declares the public JATS DTD. ElementTree
        # does not need it, so remove that declaration without fetching it.
        # Internal subsets can define entities and remain forbidden.
        if (
            not allow_external_doctype
            or declaration is None
            or "[" in declaration.group(0)
        ):
            raise ValueError(f"{label} input must not contain DOCTYPE or ENTITY declarations")
        xml_text = xml_text[: declaration.start()] + xml_text[declaration.end() :]
    try:
        return ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"invalid {label} XML: {exc}") from exc


def _normalized_text(element: ElementTree.Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _first_text(element: ElementTree.Element, local_name: str) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == local_name:
            value = _normalized_text(child)
            return value or None
    return None


def _first_element(element: ElementTree.Element, local_name: str) -> ElementTree.Element | None:
    return next(
        (child for child in element.iter() if _local_name(child.tag) == local_name),
        None,
    )


def _first_typed_text(
    element: ElementTree.Element,
    local_name: str,
    attribute: str,
    wanted: str,
) -> str | None:
    for child in element.iter():
        if (
            _local_name(child.tag) == local_name
            and child.attrib.get(attribute, "").casefold() == wanted
        ):
            return _normalized_text(child) or None
    return None


def _tei_title(element: ElementTree.Element) -> str | None:
    titles = [child for child in element.iter() if _local_name(child.tag) == "title"]
    preferred = next(
        (child for child in titles if child.attrib.get("level") in {"a", "m"}),
        titles[0] if titles else None,
    )
    return _normalized_text(preferred) or None if preferred is not None else None


def _tei_venue(element: ElementTree.Element) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) == "title" and child.attrib.get("level") in {"j", "s"}:
            return _normalized_text(child) or None
    return None


def _tei_bibl_scope(element: ElementTree.Element, wanted_units: set[str]) -> str | None:
    for child in element.iter():
        if _local_name(child.tag) != "biblScope":
            continue
        unit = (child.attrib.get("unit") or child.attrib.get("type") or "").casefold()
        if unit not in wanted_units:
            continue
        rendered = _normalized_text(child)
        if rendered:
            return rendered
        start = child.attrib.get("from")
        end = child.attrib.get("to")
        return "-".join(value for value in (start, end) if value) or None
    return None


def _tei_authors(element: ElementTree.Element) -> list[str]:
    authors: list[str] = []
    for author in element.iter():
        if _local_name(author.tag) != "author":
            continue
        surname = _first_text(author, "surname")
        forenames = [
            _normalized_text(child)
            for child in author.iter()
            if _local_name(child.tag) == "forename" and _normalized_text(child)
        ]
        rendered = ", ".join(value for value in (surname, " ".join(forenames)) if value)
        if not rendered:
            rendered = _normalized_text(author)
        if rendered:
            authors.append(rendered)
    return authors


def _strip_latex_comments(value: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in value.splitlines())


def _latex_context(value: str, start: int, end: int) -> str:
    paragraph_start = value.rfind("\n\n", 0, start)
    paragraph_end = value.find("\n\n", end)
    paragraph_start = 0 if paragraph_start < 0 else paragraph_start + 2
    paragraph_end = len(value) if paragraph_end < 0 else paragraph_end
    rendered = " ".join(value[paragraph_start:paragraph_end].split())
    return rendered[:MAX_CONTEXT_CHARS]


def _jats_pages(element: ElementTree.Element) -> str | None:
    page_range = _first_text(element, "page-range")
    if page_range:
        return page_range
    first = _first_text(element, "fpage")
    last = _first_text(element, "lpage")
    return "-".join(value for value in (first, last) if value) or None


def _bibtex_entries(value: str) -> list[tuple[str, str, str]]:
    """Split BibTeX without treating nested title braces as entry delimiters."""

    entries: list[tuple[str, str, str]] = []
    cursor = 0
    while True:
        marker = value.find("@", cursor)
        if marker < 0:
            break
        header = re.match(r"@\s*([A-Za-z]+)\s*([({])", value[marker:])
        if header is None:
            cursor = marker + 1
            continue
        entry_type = header.group(1).casefold()
        opening = header.group(2)
        closing = "}" if opening == "{" else ")"
        content_start = marker + header.end()
        depth = 1
        brace_depth = 0
        quoted = False
        escaped = False
        position = content_start
        while position < len(value):
            character = value[position]
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = not quoted
            elif not quoted:
                if opening == "{" and character == "{":
                    depth += 1
                elif opening == "{" and character == "}":
                    depth -= 1
                elif opening == "(" and character == "{":
                    brace_depth += 1
                elif opening == "(" and character == "}" and brace_depth:
                    brace_depth -= 1
                elif opening == "(" and character == "(" and brace_depth == 0:
                    depth += 1
                elif opening == "(" and character == closing and brace_depth == 0:
                    depth -= 1
                if depth == 0:
                    break
            position += 1
        if depth:
            raise ValueError(
                f"unbalanced BibTeX {opening}{closing} entry starting at character {marker}"
            )
        entries.append(
            (
                entry_type,
                value[content_start:position].strip(),
                value[marker : position + 1],
            )
        )
        cursor = position + 1
    return entries


def _parse_bibtex_fields(value: str) -> tuple[dict[str, str], list[str]]:
    fields: dict[str, str] = {}
    warnings: list[str] = []
    for field in _split_bibtex_fields(value):
        name, separator, raw_value = field.partition("=")
        normalized_name = name.strip().casefold()
        if not separator or not normalized_name:
            warnings.append(f"malformed field skipped: {' '.join(field.split())}")
            continue
        fields[normalized_name] = _clean_bibtex_value(raw_value)
    return fields, warnings


def _split_bibtex_fields(value: str) -> list[str]:
    fields: list[str] = []
    start = 0
    brace_depth = 0
    quoted = False
    escaped = False
    for position, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
        elif character == '"':
            quoted = not quoted
        elif not quoted and character == "{":
            brace_depth += 1
        elif not quoted and character == "}" and brace_depth:
            brace_depth -= 1
        elif not quoted and character == "," and brace_depth == 0:
            if field := value[start:position].strip():
                fields.append(field)
            start = position + 1
    if quoted or brace_depth:
        raise ValueError("unbalanced quoted or braced BibTeX field value")
    if field := value[start:].strip().rstrip(",").strip():
        fields.append(field)
    return fields


def _clean_bibtex_value(value: str) -> str:
    parts = re.split(r"\s*#\s*", value.strip())
    cleaned_parts: list[str] = []
    for part in parts:
        part = part.strip()
        if len(part) >= 2 and (
            (part.startswith("{") and part.endswith("}"))
            or (part.startswith('"') and part.endswith('"'))
        ):
            part = part[1:-1]
        # Braces preserve capitalization in BibTeX but are not part of the text.
        part = part.replace("{", "").replace("}", "")
        part = part.replace("~", " ").replace(r"\&", "&")
        cleaned_parts.append(part)
    return " ".join("".join(cleaned_parts).split())


def _optional_clean_bibtex_value(value: str | None) -> str | None:
    cleaned = _clean_bibtex_value(value) if value is not None else ""
    return cleaned or None


def _bibtex_doi(fields: dict[str, str], raw_entry: str) -> str | None:
    candidates = [fields.get("doi", ""), fields.get("url", ""), raw_entry]
    for candidate in candidates:
        if match := DOI_PATTERN.search(candidate):
            return normalize_doi(match.group(0).rstrip(".,;"))
    return None


def _first_pub_id(element: ElementTree.Element, id_type: str) -> str | None:
    for child in element.iter():
        if (
            _local_name(child.tag) == "pub-id"
            and child.attrib.get("pub-id-type", "").casefold() == id_type
        ):
            return _normalized_text(child) or None
    return None


def _authors(element: ElementTree.Element) -> list[str]:
    authors: list[str] = []
    for name in element.iter():
        if _local_name(name.tag) != "name":
            continue
        surname = _first_text(name, "surname")
        given = _first_text(name, "given-names")
        rendered = ", ".join(value for value in (surname, given) if value)
        if rendered:
            authors.append(rendered)
    return authors
