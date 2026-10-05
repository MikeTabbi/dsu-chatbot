"""Split each extracted page in data/extracted/ into answer-sized chunks with source metadata."""

import argparse
import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ingestion.extract import _word_count

DEFAULT_INPUT_DIR = Path("data/extracted")
DEFAULT_OUTPUT_DIR = Path("data/chunks")
MAX_WORDS = 300  # a section longer than this is split into pieces
OVERLAP_WORDS = 50  # trailing text repeated at the start of the next piece of the same section
# Bump when a code change would chunk the same extracted page differently (sizes, overlap, IDs,
# Chunk fields). The pipeline then re-chunks every page, even ones not due a check.
CHUNKER_VERSION = 1

HEADING = re.compile(r"^(#{1,6}) (.+)$")
LIST_ITEM = re.compile(r"^(- |\d+\. )")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

log = logging.getLogger(__name__)


@dataclass
class Chunk:
    chunk_id: str
    chunk_index: int
    source_url: str
    title: str
    heading_path: str  # e.g. "Housing & Dining > More than a Place to Sleep"
    modified_time: str | None
    topic: str
    low_text: bool
    word_count: int
    text: str  # Markdown, without the heading lines (those are in heading_path)


@dataclass(frozen=True)
class _Unit:
    """Text the splitter never cuts: a whole block, table row, list item, line or sentence."""

    text: str
    block: int  # index of the block it came from
    joiner: str  # separator from the previous unit of the same block
    header: str | None = None  # table header and separator lines, repeated in every piece


def chunk_page(page: dict, max_words: int = MAX_WORDS, overlap: int = OVERLAP_WORDS) -> list[Chunk]:
    """Chunk one extracted page: by Markdown heading first, then by size."""
    source_url = page.get("canonical_url") or page.get("final_url") or page["url"]
    title = page.get("title") or source_url
    chunks: list[Chunk] = []
    seen: dict[tuple[str, str], int] = {}

    for path, blocks in _sections(page["text"]):
        heading_path = " > ".join(path) or title
        for text in _split(blocks, max_words, overlap):
            occurrence = seen.get((heading_path, text), 0)  # identical text twice on one page
            seen[(heading_path, text)] = occurrence + 1
            chunks.append(
                Chunk(
                    chunk_id=chunk_id(source_url, heading_path, text, occurrence),
                    chunk_index=len(chunks),
                    source_url=source_url,
                    title=title,
                    heading_path=heading_path,
                    modified_time=page.get("modified_time"),
                    topic=page.get("topic", ""),
                    low_text=page.get("low_text", False),
                    word_count=_word_count(text),
                    text=text,
                )
            )
    return chunks


def chunk_id(source_url: str, heading_path: str, text: str, occurrence: int = 0) -> str:
    """Content hash: the same chunk of the same page gets the same ID on every run."""
    key = "\0".join([source_url, heading_path, text, str(occurrence)])
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def chunk_file(extracted_path: Path, output_dir: Path) -> list[Chunk]:
    """Chunk the page an extractor .json file holds into output_dir, under the same name."""
    page = json.loads(extracted_path.read_text())
    chunks = chunk_page(page)
    output = {"url": page["url"], "chunks": [asdict(c) for c in chunks]}
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / extracted_path.name).write_text(json.dumps(output, indent=2) + "\n")
    return chunks


def chunk_all(
    input_dir: Path | str = DEFAULT_INPUT_DIR, output_dir: Path | str = DEFAULT_OUTPUT_DIR
) -> dict[str, list[Chunk]]:
    """Chunk every page the extractor saved in input_dir. Returns chunks by file name."""
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    return {path.name: chunk_file(path, output_dir) for path in sorted(input_dir.glob("*.json"))}


def _sections(markdown: str) -> list[tuple[list[str], list[str]]]:
    """Group the extractor's blocks under their heading path. Headings with no body are dropped."""
    sections: list[tuple[list[str], list[str]]] = []
    stack: list[tuple[int, str]] = []  # (level, heading text)
    blocks: list[str] = []

    def flush():
        if blocks:
            sections.append(([text for _, text in stack], blocks.copy()))
        blocks.clear()

    for block in markdown.split("\n\n"):
        if not block.strip():
            continue
        if match := HEADING.match(block):
            flush()
            level = len(match[1])
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match[2].strip()))
        else:
            blocks.append(block)
    flush()
    return sections


def _split(blocks: list[str], max_words: int, overlap: int) -> list[str]:
    """Pack a section's blocks into pieces of at most max_words, cutting only between units."""
    units = [unit for i, block in enumerate(blocks) for unit in _units(block, i, max_words)]
    pieces: list[list[_Unit]] = []
    current: list[_Unit] = []
    for unit in units:
        if current and _word_count(_render([*current, unit])) > max_words:
            pieces.append(current)
            current = _overlap(current, overlap)
            if _word_count(_render([*current, unit])) > max_words:
                current = []
        current.append(unit)
    if current:
        pieces.append(current)
    return [_render(piece) for piece in pieces]


def _units(block: str, index: int, max_words: int) -> list[_Unit]:
    if _word_count(block) <= max_words:
        return [_Unit(block, index, "\n\n")]
    lines = block.split("\n")
    if _is_table(lines):
        # Rows are never cut; each piece of the table repeats the header row.
        header = "\n".join(lines[:2])
        return [_Unit(row, index, "\n", header) for row in lines[2:]]
    if LIST_ITEM.match(lines[0]):
        parts, joiner = _list_items(lines), "\n"
    elif len(lines) > 1:
        parts, joiner = lines, "\n"
    else:
        parts, joiner = _sentences(block, max_words), " "
    units = []
    for part in parts:
        if _word_count(part) <= max_words:
            units.append(_Unit(part, index, joiner))
        else:  # a long list item or line: fall back to sentences
            units.extend(_Unit(s, index, " ") for s in _sentences(part, max_words))
    return units


def _is_table(lines: list[str]) -> bool:
    return len(lines) > 2 and all(line.startswith("|") for line in lines) and "---" in lines[1]


def _list_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for line in lines:
        if LIST_ITEM.match(line) or not items:
            items.append(line)
        else:  # indented continuation or nested list
            items[-1] += "\n" + line
    return items


def _sentences(text: str, max_words: int) -> list[str]:
    out = []
    for sentence in SENTENCE_END.split(text.replace("\n", " ")):
        words = sentence.split(" ")
        # A "sentence" with no punctuation for max_words is cut into word windows as a last resort.
        out.extend(" ".join(words[i : i + max_words]) for i in range(0, len(words), max_words))
    return [s for s in out if s]


def _overlap(units: list[_Unit], overlap: int) -> list[_Unit]:
    """Trailing units of a piece that fit in `overlap` words. Table rows are not repeated."""
    tail: list[_Unit] = []
    for unit in reversed(units):
        if unit.header is not None or _word_count(_render([unit, *tail])) > overlap:
            break
        tail.insert(0, unit)
    return tail


def _render(units: list[_Unit]) -> str:
    text = ""
    previous = None
    for unit in units:
        if previous is None or unit.block != previous.block:
            if text:
                text += "\n\n"
            text += f"{unit.header}\n{unit.text}" if unit.header else unit.text
        else:
            text += unit.joiner + unit.text
        previous = unit
    return text


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extracted", type=Path, default=DEFAULT_INPUT_DIR, help="extractor output"
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    pages = chunk_all(args.extracted, args.out)
    for name, chunks in pages.items():
        log.info("%s: %d chunks", name, len(chunks))
    total = sum(len(c) for c in pages.values())
    print(f"Wrote {total} chunks for {len(pages)} pages to {args.out}")


if __name__ == "__main__":
    main()
