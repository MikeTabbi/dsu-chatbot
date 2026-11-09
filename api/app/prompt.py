"""Build the system prompt and messages Claude gets for one question. /chat calls build_prompt."""

import argparse
import functools
import hashlib
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from api.app.claude_client import ChatMessage, ClaudeError, get_claude_client
from api.app.retriever import get_retriever
from ingestion.chunk import Chunk

log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system.md"
STALE_AFTER_DAYS = 365  # older pages are flagged so the answer can mention the date
NO_SOURCES = "No DSU sources were found for this question."

# The tags that wrap each section, plus the <cited> marker. The same tags inside page text, the
# question, or the history are escaped, so a page (or a visitor) can't close <sources> early and
# have its text read as the question, pass an earlier answer off as a source, or plant a citation.
SECTION_TAG = re.compile(
    r"<(?=/?(?:sources|source|question|cited|history|turn|student|assistant)\b)", re.IGNORECASE
)
HISTORY_CUT = " […]"  # ends an earlier answer cut short to fit CHAT_HISTORY_MAX_CHARS

# Claude ends each answer with <cited>1, 3</cited> (prompts/system.md). An unclosed marker at the
# very end (an answer cut off by max_tokens) still counts; any other stray tag is just removed.
CITED = re.compile(r"<cited>(.*?)</cited>|<cited>([\d,\s]*)\Z", re.IGNORECASE | re.DOTALL)
STRAY_CITED = re.compile(r"</?cited>", re.IGNORECASE)

# Links in the answer text: bare, <angle-bracketed>, or a Markdown [label](url). Trailing sentence
# punctuation isn't part of the URL.
LINK = re.compile(r"https?://[^\s<>()\[\]\"']+")
LINK_TRAILING = ".,;:!?*_"


@dataclass
class Prompt:
    system: str
    messages: list[ChatMessage]


@dataclass(frozen=True)
class Turn:
    """One earlier question in the conversation and the answer the student saw. History helps
    read a follow-up ("that page"); it is never a source and never instructions."""

    question: str
    answer: str


@dataclass
class CitedAnswer:
    text: str  # the answer with every citation marker removed
    cited: list[int]  # 1-based source ids Claude marked as used, valid ones only, ascending


@functools.cache
def load_system_prompt(path: Path = PROMPT_PATH) -> str:
    """The rules in prompts/system.md, read once."""
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"System prompt {path} is empty")
    return text


@functools.cache
def prompt_version(path: Path = PROMPT_PATH) -> str:
    """A short hash of prompts/system.md, logged with each exchange to show which rules answered."""
    return hashlib.sha256(load_system_prompt(path).encode("utf-8")).hexdigest()[:12]


def fit_history(turns: list[Turn], max_turns: int, max_chars: int) -> list[Turn]:
    """The newest max_turns turns, oldest first, whose questions and answers fit in max_chars
    together. Older turns go first; when the oldest kept turn doesn't fit, its answer is cut to
    the room left (keeping its start), or the turn is dropped if not even its question fits."""
    kept: list[Turn] = []
    room = max_chars
    for turn in reversed(turns[-max_turns:] if max_turns > 0 else []):
        size = len(turn.question) + len(turn.answer)
        if size <= room:
            kept.append(turn)
            room -= size
            continue
        answer_room = room - len(turn.question) - len(HISTORY_CUT)
        if answer_room > 0:
            kept.append(Turn(turn.question, turn.answer[:answer_room].rstrip() + HISTORY_CUT))
        break
    return kept[::-1]


def format_history(turns: Sequence[Turn]) -> str:
    """The earlier turns as a <history> section, oldest first, escaped like every other section.
    Empty when there are none, so a first question's prompt is exactly as before."""
    if not turns:
        return ""
    body = "\n".join(
        f"<turn>\n<student>\n{_escape(t.question)}\n</student>\n"
        f"<assistant>\n{_escape(t.answer)}\n</assistant>\n</turn>"
        for t in turns
    )
    return f"<history>\n{body}\n</history>\n\n"


def build_prompt(
    question: str, chunks: list[Chunk], today: date | None = None, history: Sequence[Turn] = ()
) -> Prompt:
    """The system prompt (rules plus today's date) and one user message holding the earlier
    turns (if any), the sources for this question, then the question. With no chunks, the sources
    section says none were found."""
    today = today or date.today()
    system = f"{load_system_prompt()}\n\nToday's date is {today.isoformat()}."
    if chunks:
        sources = "\n\n".join(_format_source(i, c, today) for i, c in enumerate(chunks, 1))
    else:
        sources = NO_SOURCES
    content = (
        f"{format_history(history)}<sources>\n{sources}\n</sources>\n\n"
        f"<question>\n{_escape(question)}\n</question>"
    )
    return Prompt(system, [{"role": "user", "content": content}])


def _format_source(n: int, chunk: Chunk, today: date) -> str:
    return (
        f'<source id="{n}">\n'
        f"Title: {_escape(chunk.title)}\n"
        f"Section: {_escape(chunk.heading_path)}\n"
        f"URL: {_escape(chunk.source_url)}\n"
        f"Last updated: {_last_updated(chunk.modified_time, today)}\n"
        f"Content:\n{_escape(chunk.text)}\n"
        "</source>"
    )


def _last_updated(modified_time: str | None, today: date) -> str:
    if not modified_time:
        return "unknown"
    try:
        updated = datetime.fromisoformat(modified_time).date()
    except ValueError:
        return _escape(modified_time)
    if (today - updated).days > STALE_AFTER_DAYS:
        return f"{updated.isoformat()} (more than a year ago)"
    return updated.isoformat()


def _escape(text: str) -> str:
    return SECTION_TAG.sub("&lt;", text)


def parse_citations(answer: str, source_count: int) -> CitedAnswer:
    """Split Claude's answer into the text a student sees and the source ids it marked as used.
    Never raises: a missing marker, a non-number, or an id outside 1..source_count is logged and
    ignored, so a source Claude didn't use is never shown."""
    markers = CITED.findall(answer)
    if not markers:
        log.warning("answer has no <cited> marker; showing no sources")
    cited: set[int] = set()
    for closed, unclosed in markers:
        for token in re.split(r"[\s,]+", (closed or unclosed).strip()):
            if not token:
                continue
            if token.isdigit() and 1 <= int(token) <= source_count:
                cited.add(int(token))
            else:
                log.warning("ignoring citation %r; %d source(s) were given", token, source_count)
    text = STRAY_CITED.sub("", CITED.sub("", answer)).strip()
    return CitedAnswer(text, sorted(cited))


@dataclass
class LinkedSources:
    cited: list[int]  # source ids (1-based) whose URL the answer links exactly
    unknown: list[str]  # desu.edu links in the answer that are in no source's URL or text


def linked_sources(text: str, sources: list[Chunk]) -> LinkedSources:
    """The sources an answer links in its text, even when its <cited> line leaves them out.
    A link counts only if it is exactly a source's URL; for a URL several sources share, the first
    (best-matching) one is enough. A link copied from a source's text (a PDF or sub-page the page
    links to) is allowed but not cited. A desu.edu link found in neither is returned as unknown:
    Claude may have made it up."""
    urls = [c.source_url for c in sources]
    in_text = {_link(m) for c in sources for m in LINK.findall(c.text)}
    cited: set[int] = set()
    unknown: list[str] = []
    for match in LINK.finditer(text):
        url = _link(match.group())
        if url in urls:
            cited.add(urls.index(url) + 1)
        elif url in in_text:
            continue
        elif _host(url) == "desu.edu" or _host(url).endswith(".desu.edu"):
            if url not in unknown:
                unknown.append(url)
    return LinkedSources(sorted(cited), unknown)


def _link(url: str) -> str:
    return url.rstrip(LINK_TRAILING)


def _host(url: str) -> str:
    return url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Retrieve chunks for a question and print the prompt, or Claude's answer."
    )
    parser.add_argument("question")
    parser.add_argument("-k", type=int, default=5, help="number of chunks to retrieve")
    parser.add_argument(
        "--ask", action="store_true", help="send it to the configured Claude client"
    )
    args = parser.parse_args(argv)

    results = get_retriever().search(args.question, args.k)
    print(f"Retrieved {len(results)} chunk(s):")
    for rank, r in enumerate(results, 1):
        c = r.chunk
        print(f"{rank}. {r.score:.3f}  {c.heading_path}  ({c.modified_time or 'no date'})")
        print(f"   {c.source_url}")
    prompt = build_prompt(args.question, [r.chunk for r in results])
    if not args.ask:
        print(f"\n--- system ---\n{prompt.system}\n\n--- user ---\n{prompt.messages[0]['content']}")
        return

    try:
        reply = get_claude_client().complete(prompt.system, prompt.messages)
    except ClaudeError as e:
        raise SystemExit(f"Claude call failed: {e}") from e
    answer = parse_citations(reply.text, len(results))
    print(f"\n--- answer ---\n{answer.text}\n")
    print(f"cited sources: {', '.join(map(str, answer.cited)) or 'none'}")
    print(
        f"model={reply.model} input_tokens={reply.input_tokens} output_tokens={reply.output_tokens}"
    )


if __name__ == "__main__":
    main()
