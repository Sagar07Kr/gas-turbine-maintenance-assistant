"""Build the Chroma index from the PDF.

Usage: python ingest.py [--rebuild]
"""

import argparse
import gc
import hashlib
import json
import shutil
import sys
from pathlib import Path

from config import (
    COLLECTION_NAME,
    INDEX_INFO_FILE,
    ProviderError,
    Settings,
    get_settings,
    make_embeddings,
    quiet_libraries,
)

CACHE_FILE_NAME = "rag_cache.db"


class IngestError(Exception):
    """A problem with the input PDF that the user can fix."""


def load_pages(pdf_path: Path):
    from langchain_community.document_loaders import PyPDFLoader

    if not pdf_path.is_file():
        raise IngestError(f"PDF not found: {pdf_path}. Put it in data/ or set DOCUMENT_PATH.")
    try:
        pages = PyPDFLoader(str(pdf_path)).load()
    except Exception as exc:
        raise IngestError(f"Could not read PDF {pdf_path.name}: the file looks corrupted or is not a PDF.") from exc
    if not any(page.page_content.strip() for page in pages):
        raise IngestError(f"No extractable text in {pdf_path.name} (scanned PDF?). OCR is not supported.")
    return pages


def split_pages(pages, chunk_size: int, chunk_overlap: int):
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
    chunks = splitter.split_documents(pages)
    for chunk in chunks:
        # The only place pages become 1-based: PyPDFLoader gives 0-based PDF page indexes.
        chunk.metadata = {
            "source": Path(chunk.metadata.get("source", "")).name,
            "page": int(chunk.metadata.get("page", 0)) + 1,
        }
    return [c for c in chunks if c.page_content.strip()]


def compute_fingerprint(pdf_bytes: bytes, settings: Settings) -> str:
    recipe = f"{settings.chunk_size}|{settings.chunk_overlap}|{settings.embedding_model}".encode()
    return hashlib.sha256(pdf_bytes + b"|" + recipe).hexdigest()


def read_index_info(db_dir: Path) -> dict | None:
    info_path = db_dir / INDEX_INFO_FILE
    if not info_path.is_file():
        return None
    return json.loads(info_path.read_text(encoding="utf-8"))


def _write_store(chunks, embeddings, target: Path) -> None:
    from langchain_chroma import Chroma

    store = Chroma.from_documents(
        chunks,
        embeddings,
        collection_name=COLLECTION_NAME,
        persist_directory=str(target),
        ids=[f"chunk-{i}" for i in range(len(chunks))],
    )
    # Release Chroma's cached client for this path so the folder can be renamed and reused
    # (Chroma persists automatically; there is no persist() call any more).
    from chromadb.api.client import SharedSystemClient

    del store
    SharedSystemClient.clear_system_cache()
    gc.collect()


def build_index(settings: Settings | None = None, rebuild: bool = False, embeddings=None) -> dict:
    """Build the index unless the stored fingerprint already matches.

    The new index is written to a temporary folder and only swapped in after success,
    so a failed build leaves the old index untouched.
    """
    settings = settings or get_settings()
    pages = load_pages(settings.document_path)
    fingerprint = compute_fingerprint(settings.document_path.read_bytes(), settings)
    db_dir = settings.db_dir

    existing = read_index_info(db_dir)
    if existing and existing.get("fingerprint") == fingerprint and not rebuild:
        print(f"Index is up to date (fingerprint {fingerprint[:12]}). Nothing to do. Use --rebuild to force.")
        return {"status": "unchanged", **existing}

    chunks = split_pages(pages, settings.chunk_size, settings.chunk_overlap)
    print(f"Pages loaded:    {len(pages)}")
    print(f"Chunks created:  {len(chunks)} (size {settings.chunk_size}, overlap {settings.chunk_overlap})")
    print(f"Embedding with:  {settings.embedding_model} ...")

    tmp_dir = db_dir.with_name(db_dir.name + ".building")
    shutil.rmtree(tmp_dir, ignore_errors=True)
    info = {
        "fingerprint": fingerprint,
        "embedding_model": settings.embedding_model,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "source": settings.document_path.name,
        "pages": len(pages),
        "chunks": len(chunks),
    }
    try:
        _write_store(chunks, embeddings or make_embeddings(settings), tmp_dir)
        (tmp_dir / INDEX_INFO_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    old_dir = db_dir.with_name(db_dir.name + ".old")
    shutil.rmtree(old_dir, ignore_errors=True)
    if db_dir.exists():
        db_dir.rename(old_dir)
    tmp_dir.rename(db_dir)
    shutil.rmtree(old_dir, ignore_errors=True)

    # A new index makes any cached answers stale.
    cache_file = db_dir.parent / CACHE_FILE_NAME
    if cache_file.exists():
        cache_file.unlink()

    print(f"Database stored: {db_dir}")
    return {"status": "built", **info}


def main() -> int:
    quiet_libraries()
    parser = argparse.ArgumentParser(description="Build the Chroma index from the PDF.")
    parser.add_argument("--rebuild", action="store_true", help="rebuild even if the fingerprint matches")
    args = parser.parse_args()
    try:
        build_index(rebuild=args.rebuild)
    except (IngestError, ProviderError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
