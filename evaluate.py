"""Run eval_questions.json through the same pipeline as ask.py and write results/evaluation.md."""

import json
import sys
import time

from ask import NOT_FOUND, answer_question
from config import BASE_DIR, get_settings, quiet_libraries

DELAY_SECONDS = 2.0  # small gap between questions to stay under free-tier rate limits


def normalize(text: str) -> str:
    return " ".join(text.lower().replace(",", "").split())


def evaluate_in_doc(item: dict, result) -> dict:
    pages = [s["page"] for s in result.sources]
    answer_ok = result.found and all(normalize(f) in normalize(result.answer) for f in item["key_facts"])
    page_ok = result.found and any(p in pages for p in item["expected_pages"])
    return {"answer_ok": answer_ok, "page_ok": page_ok, "pages": pages}


def cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def pct(part: int, total: int) -> str:
    return f"{part}/{total} ({100 * part / total:.0f}%)" if total else "n/a"


def main() -> int:
    quiet_libraries()
    data = json.loads((BASE_DIR / "eval_questions.json").read_text(encoding="utf-8"))
    settings = get_settings()
    rows, in_rows, out_rows = [], [], []

    for item in data["in_document"]:
        result = answer_question(item["question"])
        check = evaluate_in_doc(item, result)
        in_rows.append((item, result, check))
        passed = check["answer_ok"] and check["page_ok"]
        rows.append([item["question"], f"{', '.join(item['key_facts'])} (p. {', '.join(map(str, item['expected_pages']))})",
                     result.answer, ", ".join(map(str, check["pages"])) or "-", "PASS" if passed else "FAIL",
                     f"{result.top_distance:.3f}" if result.top_distance is not None else "-"])
        print(f"[{'PASS' if passed else 'FAIL'}] {item['question']}")
        time.sleep(DELAY_SECONDS)

    for item in data["out_of_document"]:
        result = answer_question(item["question"])
        passed = result.answer == NOT_FOUND and not result.sources
        out_rows.append((item, result, passed))
        rows.append([f"{item['question']} ({item['type']})", NOT_FOUND, result.answer,
                     ", ".join(str(s["page"]) for s in result.sources) or "-", "PASS" if passed else "FAIL",
                     f"{result.top_distance:.3f}" if result.top_distance is not None else "-"])
        print(f"[{'PASS' if passed else 'FAIL'}] {item['question']}")
        time.sleep(DELAY_SECONDS)

    n_in, n_out = len(in_rows), len(out_rows)
    summary = [
        ("Answer correct (in-document, all key facts present)", pct(sum(c["answer_ok"] for _, _, c in in_rows), n_in)),
        ("Page correct (in-document, expected page returned)", pct(sum(c["page_ok"] for _, _, c in in_rows), n_in)),
        ("Not found correct (out-of-document)", pct(sum(p for _, _, p in out_rows), n_out)),
        ("False 'Not found' (in-document)", pct(sum(not r.found for _, r, _ in in_rows), n_in)),
    ]
    in_d = [r.top_distance for _, r, _ in in_rows if r.top_distance is not None]
    out_d = [r.top_distance for _, r, _ in out_rows if r.top_distance is not None]

    lines = [
        "# Evaluation results", "",
        f"Chat model: `{settings.chat_model}` | Embedding model: `{settings.embedding_model}` | "
        f"chunk size {settings.chunk_size}, overlap {settings.chunk_overlap}, top-k {settings.top_k} | "
        f"pre-filter: {settings.relevance_max_distance if settings.relevance_max_distance is not None else 'off'}", "",
        "## Summary", "", "| Metric | Result |", "|---|---|",
        *[f"| {name} | {value} |" for name, value in summary], "",
        "## Top-1 retrieval distance (L2, lower = closer)", "",
        f"- In-document: min {min(in_d):.3f}, max {max(in_d):.3f}" if in_d else "- In-document: n/a",
        f"- Out-of-document: min {min(out_d):.3f}, max {max(out_d):.3f}" if out_d else "- Out-of-document: n/a", "",
        "## Details", "",
        "| Question | Expected | Actual answer | Pages returned | Result | Top-1 distance |",
        "|---|---|---|---|---|---|",
        *["| " + " | ".join(cell(c) for c in row) + " |" for row in rows],
    ]
    out_path = BASE_DIR / "results" / "evaluation.md"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\nSummary")
    for name, value in summary:
        print(f"  {name}: {value}")
    if in_d and out_d:
        print(f"  Top-1 distance: in-doc {min(in_d):.3f}-{max(in_d):.3f}, out-of-doc {min(out_d):.3f}-{max(out_d):.3f}")
    print(f"Report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
