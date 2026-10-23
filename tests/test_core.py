import re
import subprocess
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

import ask
import ingest
from ask import NOT_FOUND, Assistant, LLMAnswer, postprocess
from conftest import ROOT, docs_for, make_pdf


class FakeStore:
    def __init__(self, docs):
        self.docs = docs
        self.calls = 0

    def similarity_search_with_score(self, question, k=3):
        self.calls += 1
        return [(d, 0.5 + i * 0.1) for i, d in enumerate(self.docs[:k])]


def assistant_with(answer: LLMAnswer, docs):
    return Assistant(FakeStore(docs), RunnableLambda(lambda _: answer), top_k=3)


# 1. "Not found" mapping
def test_found_false_returns_exact_not_found():
    result = postprocess(LLMAnswer(found=False, answer="I could not find it", used_chunks=[1]), docs_for([3]))
    assert (result.answer, result.found, result.sources) == (NOT_FOUND, False, [])


@pytest.mark.parametrize("text", ["not found.", "NOT FOUND", " Not Found! "])
def test_not_found_variants_map_to_exact_string(text):
    result = postprocess(LLMAnswer(found=True, answer=text, used_chunks=[1]), docs_for([3]))
    assert result.answer == "Not found" and result.sources == [] and result.found is False


# 2. Sources from metadata, de-duplicated, sorted, invalid labels ignored
def test_sources_from_metadata_sorted_and_deduplicated():
    docs = docs_for([12, 4, 12])
    result = postprocess(LLMAnswer(found=True, answer="24,000 hrs", used_chunks=[1, 2, 3, 99]), docs)
    assert [s["page"] for s in result.sources] == [4, 12]
    assert all(s["filename"] == "manual.pdf" and len(s["excerpt"]) <= 203 for s in result.sources)


def test_model_cannot_introduce_unretrieved_page():
    result = postprocess(LLMAnswer(found=True, answer="x", used_chunks=[2, 7]), docs_for([5, 9]))
    assert [s["page"] for s in result.sources] == [9]


def test_verified_quote_becomes_excerpt_and_fake_quote_is_ignored():
    docs = docs_for([4])
    real = postprocess(LLMAnswer(found=True, answer="x", used_chunks=[1], evidence_quote="text on page 4."), docs)
    assert real.sources[0]["excerpt"] == "text on page 4."
    fake = postprocess(LLMAnswer(found=True, answer="x", used_chunks=[1], evidence_quote="invented sentence"), docs)
    assert fake.sources[0]["excerpt"].startswith("Chunk text on page 4.")


def test_neighbors_on_same_page_are_joined_others_ignored():
    from langchain_core.documents import Document

    class StoreWithGet:
        def get(self, ids, include):
            data = {"chunk-4": ("HEADER 6F 7F", {"page": 33}), "chunk-6": ("next page text", {"page": 34})}
            found = [i for i in ids if i in data]
            return {"ids": found, "documents": [data[i][0] for i in found], "metadatas": [data[i][1] for i in found]}

    doc = Document(page_content="Combustion 32000/900 24000/900", metadata={"source": "m.pdf", "page": 33}, id="chunk-5")
    [expanded] = ask.expand_with_neighbors(StoreWithGet(), [doc])
    assert expanded.page_content == "HEADER 6F 7F\nCombustion 32000/900 24000/900"
    assert expanded.metadata["page"] == 33


def test_section_lookup_is_added_to_sources():
    result = postprocess(LLMAnswer(found=True, answer="x", used_chunks=[1]), docs_for([7]),
                         section_lookup=lambda page, text: f"Section of page {page}")
    assert result.sources[0]["section"] == "Section of page 7"


# 3. Fallback to all retrieved chunks
def test_invalid_labels_fall_back_to_all_chunks():
    result = postprocess(LLMAnswer(found=True, answer="x", used_chunks=[0, 42]), docs_for([8, 2]))
    assert [s["page"] for s in result.sources] == [2, 8]


def test_assistant_end_to_end_with_fake_llm():
    result = assistant_with(LLMAnswer(found=True, answer="1.5", used_chunks=[1]), docs_for([11])).ask("factor?")
    assert result.found and result.sources[0]["page"] == 11


def test_blank_question_rejected():
    with pytest.raises(ask.BlankQuestionError):
        assistant_with(LLMAnswer(found=False, answer="", used_chunks=[]), docs_for([1])).ask("   ")


# 4. Ingest defaults, chunk count, fingerprint skip/rebuild, failed rebuild keeps old index
def test_chunk_defaults(monkeypatch):
    for name in ("CHUNK_SIZE", "CHUNK_OVERLAP"):
        monkeypatch.delenv(name, raising=False)
    settings = ingest.get_settings()
    assert (settings.chunk_size, settings.chunk_overlap) == (500, 50)


def test_ingest_builds_then_skips_then_rebuilds(tmp_settings, fake_embeddings, capsys, monkeypatch):
    first = ingest.build_index(tmp_settings, embeddings=fake_embeddings)
    out = capsys.readouterr().out
    assert first["status"] == "built" and f"Chunks created:  {first['chunks']}" in out
    assert ingest.read_index_info(tmp_settings.db_dir)["chunks"] == first["chunks"]
    pages = sorted({int(m["page"]) for m in _stored_metadata(tmp_settings, fake_embeddings)})
    assert pages == [1, 2]  # stored pages are 1-based

    assert ingest.build_index(tmp_settings, embeddings=fake_embeddings)["status"] == "unchanged"
    assert "Nothing to do" in capsys.readouterr().out

    monkeypatch.setenv("CHUNK_SIZE", "300")
    changed = ingest.build_index(ingest.get_settings(), embeddings=fake_embeddings)
    assert changed["status"] == "built" and changed["chunk_size"] == 300


def test_failed_rebuild_leaves_old_index(tmp_settings, fake_embeddings):
    ingest.build_index(tmp_settings, embeddings=fake_embeddings)
    before = ingest.read_index_info(tmp_settings.db_dir)

    class Broken:
        def embed_documents(self, texts):
            raise RuntimeError("boom")

        def embed_query(self, text):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        ingest.build_index(tmp_settings, rebuild=True, embeddings=Broken())
    assert ingest.read_index_info(tmp_settings.db_dir) == before
    assert not tmp_settings.db_dir.with_name("db.building").exists()


def _stored_metadata(settings, embeddings):
    store = ask.load_vectorstore(settings, embeddings)
    return store.get(include=["metadatas"])["metadatas"]


# 5. PDF errors
def test_missing_pdf(tmp_settings, tmp_path):
    with pytest.raises(ingest.IngestError, match="PDF not found"):
        ingest.load_pages(tmp_path / "nope.pdf")


def test_corrupted_pdf(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a pdf")
    with pytest.raises(ingest.IngestError, match="corrupted"):
        ingest.load_pages(bad)


def test_no_text_pdf(tmp_path):
    empty = make_pdf(tmp_path / "empty.pdf", ["", ""])
    with pytest.raises(ingest.IngestError, match="No extractable text"):
        ingest.load_pages(empty)


# 6. Missing index and embedding model mismatch
def test_missing_index(tmp_settings, fake_embeddings):
    with pytest.raises(ask.IndexNotBuiltError, match="python ingest.py"):
        ask.load_vectorstore(tmp_settings, fake_embeddings)


def test_embedding_model_mismatch(tmp_settings, fake_embeddings, monkeypatch):
    ingest.build_index(tmp_settings, embeddings=fake_embeddings)
    monkeypatch.setenv("GEMINI_EMBEDDING_MODEL", "other-model")
    with pytest.raises(ask.EmbeddingModelMismatchError, match="--rebuild"):
        ask.load_vectorstore(ingest.get_settings(), fake_embeddings)


# 7. Secrets
def test_no_key_like_strings_in_sources():
    pattern = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
    files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".py", ".html", ".md", ".txt", ".json", ".example"}
             and ".venv" not in p.parts and "db" not in p.parts]
    assert files
    assert not [p for p in files if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))]


def test_env_is_gitignored():
    lines = (ROOT / ".gitignore").read_text().splitlines()
    assert ".env" in lines
