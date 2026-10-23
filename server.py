"""FastAPI server for the assistant. Run: uvicorn server:app --reload --port 8000"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

import ask
from config import BASE_DIR, ProviderError, RateLimitError, quiet_libraries

quiet_libraries()
logger = logging.getLogger("server")

app = FastAPI(title="Gas Turbine Maintenance Assistant")

# Permissive CORS for local development only (file:// pages send Origin: null). Tighten before deploying.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class AskRequest(BaseModel):
    question: str


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    first = exc.errors()[0] if exc.errors() else {}
    field = ".".join(str(p) for p in first.get("loc", []) if p != "body") or "body"
    return error_response(422, "validation_error", f"Invalid request: {field} - {first.get('msg', 'invalid value')}")


@app.exception_handler(StarletteHTTPException)
async def http_handler(request: Request, exc: StarletteHTTPException):
    return error_response(exc.status_code, "http_error", str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.error("Unexpected error: %s", type(exc).__name__)
    return error_response(500, "internal_error", "Unexpected server error.")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html", media_type="text/html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask")
def ask_endpoint(body: AskRequest):
    if not body.question.strip():
        return error_response(400, "blank_question", "Please type a question.")
    try:
        result = ask.answer_question(body.question)
    except ask.BlankQuestionError:
        return error_response(400, "blank_question", "Please type a question.")
    except ask.IndexNotBuiltError as exc:
        return error_response(409, "index_not_built", str(exc))
    except ask.EmbeddingModelMismatchError as exc:
        return error_response(409, "embedding_model_mismatch", str(exc))
    except RateLimitError as exc:
        return error_response(429, "provider_rate_limited", str(exc))
    except ProviderError as exc:
        return error_response(502, "provider_error", str(exc))
    return result.to_api()
