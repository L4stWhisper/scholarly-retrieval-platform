from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from scholarly_retrieval.reference_extraction import (
    extract_bibtex_references,
    extract_jats_references,
    extract_latex_references,
    extract_pdf_references_with_grobid,
    extract_references_from_text,
    extract_scanned_pdf_references_with_ocr_and_grobid,
    extract_tei_references,
    ocr_pdf_with_ocrmypdf,
)


def _mock_ocr_temporary_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep command-adapter tests deterministic in restricted CI sandboxes."""

    stored: dict[str, bytes] = {}

    class FakeTemporaryDirectory:
        def __enter__(self) -> str:
            return "fixture-ocr-directory"

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        "scholarly_retrieval.reference_extraction.tempfile.TemporaryDirectory",
        lambda **_kwargs: FakeTemporaryDirectory(),
    )
    monkeypatch.setattr(
        Path,
        "write_bytes",
        lambda path, value: stored.__setitem__(str(path), value),
    )
    monkeypatch.setattr(Path, "read_bytes", lambda path: stored[str(path)])
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) in stored)

JATS = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <body><sec><title>Background</title><p>Prior work
    <xref ref-type="bibr" rid="R1">[1]</xref> and again
    <xref ref-type="bibr" rid="R1 R3">[1,3]</xref>.</p></sec></body>
  <back><ref-list>
    <ref id="R1"><element-citation>
      <person-group><name><surname>Lovelace</surname><given-names>Ada</given-names></name></person-group>
      <article-title>Analytical engines</article-title><source>Computing Notes</source>
      <year>1843</year><volume>2</volume><issue>1</issue><fpage>10</fpage><lpage>20</lpage>
      <pub-id pub-id-type="doi">https://doi.org/10.1234/ENGINE.</pub-id>
    </element-citation></ref>
    <ref id="R2"><mixed-citation>Uncited additional reading, 2020.</mixed-citation></ref>
  </ref-list></back>
</article>"""


def test_jats_uses_body_callouts_and_does_not_promote_whole_bibliography() -> None:
    result = extract_jats_references(JATS)

    assert result.bibliography_entry_count == 2
    assert result.cited_entry_count == 1
    assert result.uncited_reference_ids == ["R2"]
    assert len(result.references) == 1
    reference = result.references[0]
    assert reference.reference_id == "R1"
    assert reference.callout_count == 2
    assert reference.evidence_level == "verified_anchor"
    assert reference.title == "Analytical engines"
    assert reference.authors == ["Lovelace, Ada"]
    assert reference.publication_year == 1843
    assert reference.doi == "10.1234/engine"
    assert reference.venue == "Computing Notes"
    assert reference.volume == "2"
    assert reference.issue == "1"
    assert reference.pages == "10-20"
    assert len(reference.contexts) == 2
    assert reference.contexts[0].anchor_text == "[1]"
    assert reference.contexts[0].section_title == "Background"
    assert "Prior work [1]" in reference.contexts[0].text
    assert result.warnings == ["body callout target not found in bibliography: R3"]


def test_jats_can_return_bibliography_only_entries_with_lower_evidence() -> None:
    result = extract_jats_references(JATS, include_uncited=True)

    uncited = next(item for item in result.references if item.reference_id == "R2")
    assert uncited.cited_in_text is False
    assert uncited.evidence_level == "bibliography_only"
    assert uncited.callout_count == 0


def test_jats_keeps_an_empty_anchor_as_a_bounded_auditable_context() -> None:
    result = extract_jats_references(
        '<article><body><xref ref-type="bibr" rid="R1"/></body>'
        '<back><ref-list><ref id="R1"><mixed-citation>Paper</mixed-citation>'
        "</ref></ref-list></back></article>"
    )

    assert result.references[0].contexts[0].text == "[citation anchor]"
    assert result.references[0].contexts[0].anchor_text is None


@pytest.mark.parametrize(
    "xml",
    [
        "<!DOCTYPE article [<!ENTITY xxe SYSTEM 'file:///etc/passwd'>]><article/>",
        "<article><broken></article>",
    ],
)
def test_jats_rejects_declarations_and_malformed_xml(xml: str) -> None:
    with pytest.raises(ValueError):
        extract_jats_references(xml)


def test_jats_accepts_external_public_doctype_without_resolving_it() -> None:
    xml = """<!DOCTYPE article PUBLIC "-//NLM//DTD JATS 1.2//EN" "JATS.dtd">
    <article><body><p>See <xref ref-type="bibr" rid="R1">1</xref>.</p></body>
    <back><ref-list><ref id="R1"><element-citation><article-title>Safe JATS</article-title>
    <year>2024</year></element-citation></ref></ref-list></back></article>"""

    result = extract_jats_references(xml)

    assert result.references[0].title == "Safe JATS"
    assert result.references[0].evidence_level == "verified_anchor"


def test_tei_links_body_anchors_to_grobid_bibliography() -> None:
    tei = """<TEI xmlns="http://www.tei-c.org/ns/1.0">
      <text><body><div><head>Prior work</head><p>Prior <ref type="bibr" target="#b0">[1]</ref>
      and missing <ref type="bibr" target="#b9">[9]</ref>.</p></div></body>
      <back><listBibl><biblStruct xml:id="b0">
        <analytic><author><persName><forename>Ada</forename><surname>Lovelace</surname>
        </persName></author><title level="a">Analytical Engines</title></analytic>
        <monogr><title level="j">Computing Notes</title><imprint><date when="1843"/>
        <biblScope unit="volume">2</biblScope><biblScope unit="issue">1</biblScope>
        <biblScope unit="page" from="10" to="20"/></imprint></monogr>
        <idno type="DOI">10.1234/ENGINE</idno>
      </biblStruct><biblStruct xml:id="b1"><note>Uncited</note></biblStruct>
      </listBibl></back></text></TEI>"""

    result = extract_tei_references(tei)
    assert result.source_format == "tei"
    assert result.bibliography_entry_count == 2
    assert result.uncited_reference_ids == ["b1"]
    assert result.references[0].reference_id == "b0"
    assert result.references[0].title == "Analytical Engines"
    assert result.references[0].authors == ["Lovelace, Ada"]
    assert result.references[0].publication_year == 1843
    assert result.references[0].doi == "10.1234/engine"
    assert result.references[0].venue == "Computing Notes"
    assert result.references[0].volume == "2"
    assert result.references[0].issue == "1"
    assert result.references[0].pages == "10-20"
    assert result.references[0].contexts[0].section_title == "Prior work"
    assert result.warnings == ["body callout target not found in bibliography: b9"]


def test_latex_links_cites_and_embedded_bibitems() -> None:
    latex = r"""
    Text \cite[p. 1]{engine,missing} and again \cite{engine}.
    \begin{thebibliography}{9}
    \bibitem{engine} Ada Lovelace. Analytical Engines. 1843.
    doi:10.1234/ENGINE.
    \bibitem{extra} Uncited work, 2020.
    \end{thebibliography}
    """
    result = extract_latex_references(latex)

    assert result.bibliography_entry_count == 2
    assert result.cited_entry_count == 1
    assert result.uncited_reference_ids == ["extra"]
    assert result.references[0].callout_count == 2
    assert result.references[0].doi == "10.1234/engine"
    assert len(result.references[0].contexts) == 2
    assert result.references[0].contexts[0].source_locator is not None
    assert r"\cite[p. 1]{engine,missing}" in result.references[0].contexts[0].text
    assert result.warnings == ["cite key not found in bibliography: missing"]


def test_bibtex_extracts_nested_fields_as_unverified_candidates() -> None:
    bibtex = r'''@string{jmlr = "Journal of Machine Learning Research"}
    @article{vaswani2017attention,
      title = {Attention Is {All} You Need},
      author = "Vaswani, Ashish and Shazeer, Noam",
      year = {2017},
      journal = {Neural Information Processing},
      volume = {30}, number = {4}, pages = {1--11},
      doi = {https://doi.org/10.48550/ARXIV.1706.03762},
    }
    @inproceedings(alphafold,
      title = "Highly accurate protein structure prediction with {AlphaFold}",
      author = {Jumper, John and Evans, Richard},
      year = 2021,
      url = {https://doi.org/10.1038/s41586-021-03819-2}
    )'''

    result = extract_bibtex_references(bibtex)

    assert result.source_format == "bibtex"
    assert result.bibliography_entry_count == 2
    assert result.cited_entry_count == 0
    assert result.uncited_reference_ids == ["vaswani2017attention", "alphafold"]
    assert all(item.evidence_level == "bibliography_only" for item in result.references)
    assert all(not item.cited_in_text and item.callout_count == 0 for item in result.references)
    assert result.references[0].title == "Attention Is All You Need"
    assert result.references[0].authors == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert result.references[0].doi == "10.48550/arxiv.1706.03762"
    assert result.references[0].venue == "Neural Information Processing"
    assert result.references[0].volume == "30"
    assert result.references[0].issue == "4"
    assert result.references[0].pages == "1--11"
    assert result.references[0].contexts == []
    assert result.references[1].publication_year == 2021
    assert result.references[1].doi == "10.1038/s41586-021-03819-2"


def test_bibtex_reports_duplicate_keys_and_malformed_fields() -> None:
    result = extract_bibtex_references(
        "@article{same, title={First}, broken} @book{same, title={Duplicate}}"
    )

    assert len(result.references) == 1
    assert result.bibliography_entry_count == 2
    assert result.warnings == [
        "BibTeX entry same: malformed field skipped: broken",
        "duplicate BibTeX citation key skipped: same",
    ]


def test_bibtex_rejects_unbalanced_entries() -> None:
    with pytest.raises(ValueError, match="unbalanced BibTeX"):
        extract_bibtex_references("@article{broken, title={Missing close}")


def test_text_dispatcher_rejects_pdf_and_unknown_formats() -> None:
    for source_format in ("pdf", "unknown"):
        with pytest.raises(ValueError, match="text reference format"):
            extract_references_from_text("content", source_format=source_format)


def test_pdf_grobid_adapter_validates_and_parses_tei() -> None:
    async def scenario() -> None:
        tei = """<TEI><text><body><ref type="bibr" target="#b0"/></body>
        <back><listBibl><biblStruct id="b0"><title>Paper</title></biblStruct>
        </listBibl></back></text></TEI>"""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/processFulltextDocument"
            assert request.method == "POST"
            assert b"paper.pdf" in request.content
            return httpx.Response(200, text=tei)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await extract_pdf_references_with_grobid(
            b"%PDF-fixture",
            grobid_url="http://grobid.test",
            client=client,
        )
        await client.aclose()
        assert result.source_format == "pdf-grobid-tei"
        assert result.processing_steps == ["grobid", "tei"]
        assert result.references[0].reference_id == "b0"

    asyncio.run(scenario())


def test_scanned_pdf_runs_local_ocr_before_grobid(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_ocr_temporary_files(monkeypatch)

    async def scenario() -> None:
        tei = """<TEI><text><body><p><ref type="bibr" target="#b0">[1]</ref></p></body>
        <back><listBibl><biblStruct id="b0"><title>Scanned Paper</title></biblStruct>
        </listBibl></back></text></TEI>"""

        def fake_ocr(
            arguments: list[str], **options: object
        ) -> subprocess.CompletedProcess[bytes]:
            assert arguments[:2] == ["fixture-ocrmypdf", "--skip-text"]
            assert arguments[arguments.index("--language") + 1] == "eng+chi_sim"
            assert options["shell"] is False
            input_path, output_path = map(Path, arguments[-2:])
            output_path.write_bytes(input_path.read_bytes() + b"-searchable")
            return subprocess.CompletedProcess(arguments, 0, b"", b"")

        def handler(request: httpx.Request) -> httpx.Response:
            assert b"-searchable" in request.content
            return httpx.Response(200, text=tei)

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = await extract_scanned_pdf_references_with_ocr_and_grobid(
            b"%PDF-scanned-fixture",
            grobid_url="http://grobid.test",
            ocrmypdf_command="fixture-ocrmypdf",
            ocr_language="eng+chi_sim",
            client=client,
            ocr_runner=fake_ocr,
        )
        await client.aclose()

        assert result.source_format == "ocr-pdf-grobid-tei"
        assert result.processing_steps == ["ocrmypdf", "grobid", "tei"]
        assert result.references[0].contexts[0].anchor_text == "[1]"

    asyncio.run(scenario())


def test_ocr_adapter_reports_missing_executable_and_rejects_unsafe_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_ocr_temporary_files(monkeypatch)

    def missing_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError

    with pytest.raises(ValueError, match="executable not found"):
        ocr_pdf_with_ocrmypdf(
            b"%PDF",
            command="missing-ocr",
            runner=missing_runner,
        )
    with pytest.raises(ValueError, match="OCR language"):
        ocr_pdf_with_ocrmypdf(b"%PDF", language="eng;calc")
