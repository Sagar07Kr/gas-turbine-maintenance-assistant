"""Test fixtures: fake embeddings and fake LLM chains, temp folders only (never the real db/ or API)."""

import sys
from pathlib import Path

import pytest
from fpdf import FPDF
from langchain_core.documents import Document
from langchain_core.embeddings import DeterministicFakeEmbedding

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config  # noqa: E402


def make_pdf(path: Path, pages: list[str]) -> Path:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=11)
    for text in pages:
        pdf.add_page()
        if text:
            pdf.multi_cell(0, 6, text)
    pdf.output(str(path))
    return path


@pytest.fixture
def tmp_settings(tmp_path, monkeypatch):
    """Settings pointing at a temp PDF and temp db; the API key is a dummy and never used."""
    pdf = make_pdf(
        tmp_path / "manual.pdf",
        ["Hot gas path inspection 24,000 hrs or 1200 starts. " * 30, "Peak load is defined as operation above base load. " * 30],
    )
    monkeypatch.setenv("DOCUMENT_PATH", str(pdf))
    monkeypatch.setenv("DB_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "fake-embedding")
    for name in ("CHUNK_SIZE", "CHUNK_OVERLAP", "TOP_K", "RELEVANCE_MAX_DISTANCE"):
        monkeypatch.delenv(name, raising=False)
    return config.get_settings()


@pytest.fixture
def fake_embeddings():
    return DeterministicFakeEmbedding(size=32)


def docs_for(pages: list[int]) -> list[Document]:
    return [Document(page_content=f"Chunk text on page {p}. " * 20, metadata={"source": "manual.pdf", "page": p}) for p in pages]
