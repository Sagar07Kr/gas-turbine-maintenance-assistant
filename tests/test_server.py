import re

import pytest
from fastapi.testclient import TestClient

import ask
import server
from ask import AskResult
from conftest import ROOT

client = TestClient(server.app)


@pytest.fixture
def answer_with(monkeypatch):
    def install(fn):
        monkeypatch.setattr(ask, "answer_question", fn)

    return install


def test_success_contract(answer_with):
    answer_with(lambda q: AskResult("24,000 hrs or 1200 starts", True, [{"filename": "m.pdf", "page": 10, "excerpt": "Hot gas path"}]))
    r = client.post("/ask", json={"question": "interval?"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    assert r.json() == {"answer": "24,000 hrs or 1200 starts", "found": True,
                        "sources": [{"filename": "m.pdf", "page": 10, "excerpt": "Hot gas path"}]}


def test_not_found_contract(answer_with):
    answer_with(lambda q: AskResult("Not found", False, []))
    assert client.post("/ask", json={"question": "capital of France?"}).json() == {"answer": "Not found", "found": False, "sources": []}


def test_blank_question():
    r = client.post("/ask", json={"question": "   "})
    assert r.status_code == 400 and r.json()["error"]["code"] == "blank_question"


def test_missing_question_field_uses_error_shape():
    r = client.post("/ask", json={})
    assert r.status_code == 422
    assert set(r.json()) == {"error"} and r.json()["error"]["code"] == "validation_error"


def test_index_not_built(answer_with):
    def raise_missing(q):
        raise ask.IndexNotBuiltError("The index is not built yet. Run: python ingest.py")

    answer_with(raise_missing)
    r = client.post("/ask", json={"question": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "index_not_built"


def test_unknown_route_uses_error_shape():
    r = client.get("/nope")
    assert r.status_code == 404 and "error" in r.json()


def test_unexpected_error_is_json_500(answer_with):
    def boom(q):
        raise RuntimeError("secret stack detail")

    answer_with(boom)
    r = TestClient(server.app, raise_server_exceptions=False).post("/ask", json={"question": "x"})
    assert r.status_code == 500 and r.json() == {"error": {"code": "internal_error", "message": "Unexpected server error."}}


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_root_serves_html():
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html") and "<canvas" in r.text


def test_cors_null_origin(answer_with):
    answer_with(lambda q: AskResult("Not found", False, []))
    r = client.post("/ask", json={"question": "x"}, headers={"Origin": "null"})
    assert r.headers["access-control-allow-origin"] == "*"


def test_cors_preflight():
    r = client.options("/ask", headers={"Origin": "http://localhost:5500", "Access-Control-Request-Method": "POST",
                                        "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]


def test_index_html_static_checks():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "innerHTML" not in html
    assert 'const API_URL = "http://localhost:8000/ask";' in html
    assert re.search(r"cdnjs\.cloudflare\.com/ajax/libs/three\.js/0\.170\.0/three\.module\.min\.js", html)
