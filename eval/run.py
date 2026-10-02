"""Run the eval set in eval/questions.csv: retrieval only (free), or full answers through /chat."""

import argparse
import csv
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from api.app.claude_client import ClaudeClient, FakeClaudeClient, get_claude_client
from api.app.config import settings
from api.app.main import app, get_chat_client, get_chat_retriever, get_settings
from api.app.retriever import Retriever

EVAL_DIR = Path(__file__).resolve().parent
CASES_PATH = EVAL_DIR / "questions.csv"
RESULTS_DIR = EVAL_DIR / "results"  # gitignored

BEHAVIOR_FOR_CATEGORY = {
    "answer": "answer_with_source",  # answers from the sources and links the right page
    "personal": "redirect_personal",  # can't see records; says where to check
    "gap": "say_not_found",  # says it couldn't find it and points to an office
    "off_topic": "decline",  # politely declines, offers DSU help
}

# Lowercase markers for the behavior checks. Answers are compared lowercased, with curly quotes
# straightened. These are heuristics: read the saved answers before trusting a pass or fail.
NOT_FOUND = (
    "couldn't find",
    "could not find",
    "wasn't able to find",
    "not able to find",
    "don't have information",
    "don't have details",
    "don't have specific",
    "don't have any",
    "doesn't say",
    "don't say",
    "don't see",
    "doesn't explain",
    "don't explain",
    "doesn't give",
    "don't give",
    "doesn't list",
    "don't list",
    "doesn't mention",
    "don't mention",
    "doesn't include",
    "don't include",
    "doesn't cover",
    "don't cover",
    "isn't listed",
    "not listed",
    "not covered",
    "isn't covered",
    "no information",
    "no details",
)
POINTS_TO_OFFICE = ("office", "contact", "advisor", "registrar", "admissions", "reach out")
CANT_SEE_RECORDS = (
    "can't see",
    "cannot see",
    "can't access",
    "cannot access",
    "don't have access",
    "unable to see",
    "not able to see",
    "can't look up",
    "can't check",
)
WHERE_TO_CHECK = (
    "degreeworks",
    "navigate",
    "banner",
    "registrar",
    "advisor",
    "student accounts",
    "financial aid",
)
DECLINES = ("only help", "can only", "only answer", "dsu question", "questions about dsu")

URL = re.compile(r"https?://[^\s)\]>\"'*]+")


@dataclass
class Case:
    question: str
    category: str
    expected_behavior: str
    expected_urls: list[str]
    must_contain: list[str]
    must_not_contain: list[str]
    expected_answer: str = ""


@dataclass
class Result:
    question: str
    category: str
    passed: bool | None  # None: nothing to check (retrieval mode, no expected URL)
    expected_urls: list[str]
    source_urls: list[str]  # retrieval: top k chunk URLs; full: the response's (cited) sources
    url_rank: int | None = None  # retrieval: 1-based rank of the first expected URL
    status: int | None = None
    answer: str = ""
    failures: list[str] = field(default_factory=list)


def _split(value: str) -> list[str]:
    """Split on | outside parentheses, so a regex like re:(a|b) stays one phrase."""
    parts, depth, current = [], 0, ""
    for ch in value:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if ch == "|" and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return [p.strip() for p in parts if p.strip()]


def load_cases(path: Path | str = CASES_PATH) -> list[Case]:
    """Every row of the eval CSV. Lists (source_url, must_contain, must_not_contain) are
    separated by |. A phrase starting with "re:" is a case-insensitive regex."""
    cases = []
    with open(path, newline="", encoding="utf-8") as f:
        for line, row in enumerate(csv.DictReader(f), start=2):
            case = Case(
                question=row["question"].strip(),
                category=row["category"].strip(),
                expected_behavior=row["expected_behavior"].strip(),
                expected_urls=_split(row["source_url"]),
                must_contain=_split(row["must_contain"]),
                must_not_contain=_split(row["must_not_contain"]),
                expected_answer=row["expected_answer"].strip(),
            )
            if not case.question:
                raise ValueError(f"{path}:{line}: empty question")
            if BEHAVIOR_FOR_CATEGORY.get(case.category) != case.expected_behavior:
                raise ValueError(
                    f"{path}:{line}: category {case.category!r} needs expected_behavior "
                    f"{BEHAVIOR_FOR_CATEGORY.get(case.category)!r}, got {case.expected_behavior!r}"
                    f" (categories: {', '.join(BEHAVIOR_FOR_CATEGORY)})"
                )
            cases.append(case)
    return cases


def _norm_url(url: str) -> str:
    return url.strip().rstrip(".,;:/").split("#")[0].lower()


def _norm_text(text: str) -> str:
    return text.lower().replace("’", "'").replace("‘", "'")


def _phrase_in(phrase: str, text: str) -> bool:
    """A plain phrase matches as a substring; "re:..." is a regex, for phrases like "your GPA is"
    that also appear in good answers ("I can't tell you what your GPA is")."""
    if phrase.startswith("re:"):
        return re.search(phrase[3:], text, re.IGNORECASE) is not None
    return _norm_text(phrase) in text


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def check_retrieval(case: Case, retriever: Retriever, k: int) -> Result:
    urls = [r.chunk.source_url for r in retriever.search(case.question, k)]
    result = Result(case.question, case.category, None, case.expected_urls, urls)
    if not case.expected_urls:
        return result
    expected = {_norm_url(u) for u in case.expected_urls}
    ranks = [i for i, u in enumerate(urls, 1) if _norm_url(u) in expected]
    result.url_rank = ranks[0] if ranks else None
    result.passed = bool(ranks)
    if not ranks:
        result.failures.append(f"no expected URL in the top {k}")
    return result


def check_answer(case: Case, status: int, body: dict) -> Result:
    """Grade one /chat response against the case: status, expected behavior, and phrases."""
    answer = body.get("answer") or body.get("detail") or ""
    sources = [s["url"] for s in body.get("sources", [])]
    result = Result(
        case.question,
        case.category,
        False,
        case.expected_urls,
        sources,
        status=status,
        answer=answer,
    )
    failures = result.failures
    if status != 200:
        failures.append(f"HTTP {status}")
    text = _norm_text(answer)

    if case.expected_behavior == "answer_with_source":
        cited = {_norm_url(u) for u in URL.findall(answer)}
        if case.expected_urls:
            if not cited & {_norm_url(u) for u in case.expected_urls}:
                failures.append("does not cite an expected URL")
        elif not cited & {_norm_url(u) for u in sources}:
            failures.append("does not cite a source")
        if not sources:
            failures.append("returns no cited sources")
    elif case.expected_behavior == "redirect_personal":
        if not _has_any(text, CANT_SEE_RECORDS):
            failures.append("does not say it can't see records")
        if not _has_any(text, WHERE_TO_CHECK):
            failures.append("does not say where to check")
    elif case.expected_behavior == "say_not_found":
        if not _has_any(text, NOT_FOUND):
            failures.append("does not say it couldn't find it")
        if not _has_any(text, POINTS_TO_OFFICE):
            failures.append("does not point to an office")
    elif case.expected_behavior == "decline":
        if not _has_any(text, DECLINES):
            failures.append("does not decline")

    missing = [p for p in case.must_contain if not _phrase_in(p, text)]
    forbidden = [p for p in case.must_not_contain if _phrase_in(p, text)]
    if missing:
        failures.append("missing: " + ", ".join(repr(p) for p in missing))
    if forbidden:
        failures.append("contains forbidden: " + ", ".join(repr(p) for p in forbidden))
    result.passed = not failures
    return result


def run_retrieval(cases: list[Case], retriever: Retriever, k: int) -> list[Result]:
    return [check_retrieval(c, retriever, k) for c in cases]


def run_full(
    cases: list[Case],
    client: ClaudeClient,
    retriever: Retriever | None = None,
    k: int | None = None,
) -> list[Result]:
    """Post each question to /chat in-process (the same code path as the server), with the
    given client, and grade the response. Overrides already set on the app are restored after."""
    saved = dict(app.dependency_overrides)
    app.dependency_overrides[get_chat_client] = lambda: client
    if retriever is not None:
        app.dependency_overrides[get_chat_retriever] = lambda: retriever
    if k is not None:
        app.dependency_overrides[get_settings] = lambda: settings.model_copy(
            update={"chat_top_k": k}
        )
    try:
        with TestClient(app) as http:
            results = []
            for case in cases:
                response = http.post("/chat", json={"question": case.question})
                results.append(check_answer(case, response.status_code, response.json()))
            return results
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


def _resolve(dependency: Callable, default: Callable):
    """What /chat would get for this dependency: the app's override (as the /chat tests set one)
    if there is one, else default()."""
    return app.dependency_overrides.get(dependency, default)()


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _path(url: str) -> str:
    return url.replace("https://www.desu.edu", "") or "/"


def _mark(passed: bool | None) -> str:
    return {True: "PASS", False: "FAIL", None: "-"}[passed]


def score(results: list[Result]) -> tuple[int, int]:
    graded = [r for r in results if r.passed is not None]
    return sum(r.passed for r in graded), len(graded)


def print_retrieval(results: list[Result], k: int) -> None:
    print(f"{'#':>2}  {'category':<9}  {'question':<44}  {'hit':<4}  {'rank':>4}  top result")
    for i, r in enumerate(results, 1):
        rank = str(r.url_rank) if r.url_rank else "-"
        top = _short(_path(r.source_urls[0]), 60) if r.source_urls else "(nothing retrieved)"
        print(
            f"{i:>2}  {r.category:<9}  {_short(r.question, 44):<44}  "
            f"{_mark(r.passed):<4}  {rank:>4}  {top}"
        )
    passed, graded = score(results)
    print(f"\nRetrieval: {passed}/{graded} cases have an expected URL in the top {k}", end="")
    print(f" ({len(results) - graded} cases have no expected URL)")


def print_full(results: list[Result]) -> None:
    print(f"{'#':>2}  {'category':<9}  {'question':<44}  {'HTTP':>4}  {'src':<4}  result")
    for i, r in enumerate(results, 1):
        expected = {_norm_url(u) for u in r.expected_urls}
        src = (
            "-"
            if not expected
            else ("yes" if expected & {_norm_url(u) for u in r.source_urls} else "no")
        )
        print(
            f"{i:>2}  {r.category:<9}  {_short(r.question, 44):<44}  "
            f"{r.status or '-':>4}  {src:<4}  {_mark(r.passed)}"
        )
    failed = [(i, r) for i, r in enumerate(results, 1) if r.passed is False]
    if failed:
        print("\nFailures:")
        for i, r in failed:
            print(f"{i:>2}. {r.question}: {'; '.join(r.failures)}")
    passed, graded = score(results)
    print(f"\nOverall: {passed}/{graded} passed ({passed / graded:.0%})" if graded else "")
    print("src: an expected URL was among the sources the answer cites")


def save(results: list[Result], mode: str, meta: dict, out_dir: Path = RESULTS_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{stamp}-{mode}.json"
    passed, graded = score(results)
    data = {
        "mode": mode,
        "run_at": datetime.now().isoformat(timespec="seconds"),
        **meta,
        "passed": passed,
        "graded": graded,
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def confirm(n: int, client_name: str, ask: Callable[[str], str] = input) -> bool:
    print(f"Full mode will send {n} question(s) to Claude ({client_name}). This costs money.")
    try:
        return ask("Continue? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv: list[str] | None = None, ask: Callable[[str], str] = input) -> int:
    parser = argparse.ArgumentParser(description="Run the eval set in eval/questions.csv.")
    parser.add_argument(
        "--full", action="store_true", help="answer each case through /chat (calls Claude)"
    )
    parser.add_argument(
        "--client",
        choices=["anthropic", "fake"],
        default="anthropic",
        help="Claude client for --full (default: anthropic)",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation for --full")
    parser.add_argument("-k", type=int, default=settings.chat_top_k, help="chunks per question")
    parser.add_argument(
        "--category", choices=list(BEHAVIOR_FOR_CATEGORY), help="only cases in this category"
    )
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="eval CSV")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR, help="results folder")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.category:
        cases = [c for c in cases if c.category == args.category]
    retriever = _resolve(get_chat_retriever, get_chat_retriever)

    if not args.full:
        results = run_retrieval(cases, retriever, args.k)
        print_retrieval(results, args.k)
        path = save(results, "retrieval", {"k": args.k}, args.out)
        print(f"Saved {path}")
        return 0

    config = settings.model_copy(update={"claude_client": args.client})
    client = _resolve(get_chat_client, lambda: get_claude_client(config))
    real = not isinstance(client, FakeClaudeClient)
    if real and not args.yes and not confirm(len(cases), config.claude_model, ask):
        print("Cancelled; no Claude calls made.")
        return 1
    results = run_full(cases, client, retriever, args.k)
    print_full(results)
    model = config.claude_model if real else "fake"
    path = save(results, "full", {"k": args.k, "client": args.client, "model": model}, args.out)
    print(f"Saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
