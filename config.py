"""Shared settings, Gemini factories and retry logic used by every script."""

import logging
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_PDF_NAME = "project_3_gen_Ai.pdf"
COLLECTION_NAME = "gas_turbine_manual"
INDEX_INFO_FILE = "index_info.json"

load_dotenv(BASE_DIR / ".env")


class ProviderError(RuntimeError):
    """Gemini failed after retries (bad key, retired model, outage...)."""


class RateLimitError(ProviderError):
    """Gemini kept answering 429 / quota exhausted after retries."""


@dataclass(frozen=True)
class Settings:
    document_path: Path
    db_dir: Path
    chunk_size: int
    chunk_overlap: int
    top_k: int
    chat_model: str
    embedding_model: str
    temperature: float
    relevance_max_distance: float | None


def _resolve(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else BASE_DIR / path


def get_settings() -> Settings:
    """Read settings from the environment at call time, so tests can override env vars."""
    threshold = os.getenv("RELEVANCE_MAX_DISTANCE", "").strip()
    return Settings(
        document_path=_resolve(os.getenv("DOCUMENT_PATH", f"data/{DEFAULT_PDF_NAME}")),
        db_dir=_resolve(os.getenv("DB_DIR", "db")),
        chunk_size=int(os.getenv("CHUNK_SIZE", "500")),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "50")),
        top_k=int(os.getenv("TOP_K", "5")),
        chat_model=os.getenv("GEMINI_CHAT_MODEL", "gemini-3.5-flash-lite"),
        embedding_model=os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001"),
        temperature=float(os.getenv("GEMINI_TEMPERATURE", "0.1")),
        relevance_max_distance=float(threshold) if threshold else None,
    )


def get_api_key() -> str:
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        raise ProviderError("No Gemini API key found. Set GOOGLE_API_KEY in your environment or in .env.")
    return key


def quiet_libraries() -> None:
    """Hide library warnings and debug logs so terminal output stays screenshot-clean."""
    warnings.filterwarnings("ignore")
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
    for name in ("chromadb", "httpx", "langchain", "langchain_google_genai", "google", "urllib3"):
        logging.getLogger(name).setLevel(logging.ERROR)


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__} {exc}".lower()


def is_rate_limit(exc: Exception) -> bool:
    return any(s in _error_text(exc) for s in ("429", "resource_exhausted", "resourceexhausted", "rate limit", "quota"))


def is_transient(exc: Exception) -> bool:
    markers = ("500", "502", "503", "504", "unavailable", "deadline", "timeout", "timed out", "internal", "connection")
    return is_rate_limit(exc) or any(s in _error_text(exc) for s in markers)


def safe_message(exc: Exception) -> str:
    """First line of a provider error, shortened, with any API key value masked."""
    text = (str(exc).splitlines() or [type(exc).__name__])[0][:200]
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        key = os.getenv(name)
        if key:
            text = text.replace(key, "***")
    return text


def with_retry(fn, *args, attempts: int = 5, base_delay: float = 1.0, **kwargs):
    """Call fn, retrying 429/transient errors with exponential backoff (1s, 2s, 4s, 8s).

    Non-transient errors are re-raised unchanged so the caller decides what they mean.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except ProviderError:
            raise
        except Exception as exc:  # provider SDKs raise many unrelated exception types
            if not is_transient(exc):
                raise
            last = exc
        if attempt < attempts:
            time.sleep(base_delay * 2 ** (attempt - 1))
    if is_rate_limit(last):
        raise RateLimitError("Gemini rate limit reached. Wait a minute and try again.") from last
    raise ProviderError(f"Gemini is temporarily unavailable: {safe_message(last)}") from last


class BatchedRetryingEmbeddings:
    """Wraps LangChain embeddings: small batches + retry, so free-tier limits don't break ingestion."""

    def __init__(self, inner, batch_size: int = 20, pause_seconds: float = 0.0):
        self.inner = inner
        self.batch_size = batch_size
        self.pause_seconds = pause_seconds

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            if start and self.pause_seconds:
                time.sleep(self.pause_seconds)  # stay under the per-minute embedding quota
            batch = texts[start : start + self.batch_size]
            vectors.extend(with_retry(self.inner.embed_documents, batch, base_delay=10.0))
            print(f"  embedded {min(start + self.batch_size, len(texts))}/{len(texts)}", flush=True)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return with_retry(self.inner.embed_query, text)


def make_embeddings(settings: Settings) -> BatchedRetryingEmbeddings:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings

    # Free tier allows roughly 100 embedded texts per minute: 20 texts every 12 s stays below it.
    return BatchedRetryingEmbeddings(
        GoogleGenerativeAIEmbeddings(model=settings.embedding_model, google_api_key=get_api_key()),
        batch_size=20,
        pause_seconds=float(os.getenv("EMBED_PAUSE_SECONDS", "12")),
    )


def make_chat_model(settings: Settings):
    from langchain_google_genai import ChatGoogleGenerativeAI

    # with_retry does the backoff, so the client's own retries stay low to avoid long double waits.
    return ChatGoogleGenerativeAI(
        model=settings.chat_model,
        temperature=settings.temperature,
        google_api_key=get_api_key(),
        max_retries=1,
        timeout=60,
    )
