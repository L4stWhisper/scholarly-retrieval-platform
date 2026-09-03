"""Real OCRmyPDF -> GROBID smoke, opt-in because it needs native services."""

from __future__ import annotations

import asyncio
import io
import os

import pytest

from scholarly_retrieval.reference_extraction import (
    extract_scanned_pdf_references_with_ocr_and_grobid,
)

RUN_OCR = os.getenv("SCHOLAR_RUN_OCR_TESTS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not RUN_OCR,
        reason="set SCHOLAR_RUN_OCR_TESTS=1 inside the OCR container",
    ),
]


def _synthetic_scanned_article() -> bytes:
    """Rasterize a tiny article so the PDF contains no searchable text layer."""

    image_module = pytest.importorskip("PIL.Image")
    draw_module = pytest.importorskip("PIL.ImageDraw")
    font_module = pytest.importorskip("PIL.ImageFont")
    image = image_module.new("RGB", (1654, 2339), "white")
    draw = draw_module.Draw(image)
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    title_font = font_module.truetype(font_path, 54)
    body_font = font_module.truetype(font_path, 34)
    draw.text((130, 120), "A TEST ARTICLE ABOUT TRANSFORMERS", fill="black", font=title_font)
    lines = [
        "Introduction",
        "The transformer architecture is widely used in machine learning [1].",
        "This page is a raster image that simulates a scanned paper.",
        "References",
        "[1] Ashish Vaswani et al. Attention Is All You Need.",
        "Advances in Neural Information Processing Systems, 2017.",
        "doi: 10.48550/arXiv.1706.03762",
    ]
    for index, line in enumerate(lines):
        draw.text((130, 360 + index * 105), line, fill="black", font=body_font)
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=200.0)
    return output.getvalue()


def test_real_scanned_pdf_ocr_to_grobid_reference() -> None:
    async def scenario() -> None:
        result = await extract_scanned_pdf_references_with_ocr_and_grobid(
            _synthetic_scanned_article(),
            grobid_url=os.getenv("SCHOLAR_TEST_GROBID_URL", "http://127.0.0.1:8070"),
            include_uncited=True,
            ocr_language="eng",
        )
        assert result.source_format == "ocr-pdf-grobid-tei"
        assert result.processing_steps == ["ocrmypdf", "grobid", "tei"]
        assert result.bibliography_entry_count >= 1
        assert any("attention" in reference.raw_text.casefold() for reference in result.references)

    asyncio.run(scenario())
