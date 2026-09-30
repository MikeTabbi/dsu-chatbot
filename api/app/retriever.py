"""Find the chunks most relevant to a question. /chat depends on the Retriever interface only."""

import argparse
import json
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from api.app.config import Settings, settings
from ingestion.chunk import DEFAULT_OUTPUT_DIR as CHUNKS_DIR
from ingestion.chunk import Chunk

LOW_TEXT_PENALTY = 0.8  # low-text chunks keep 80% of their score, so they lose close calls
MIN_SCORE = 0.1  # words found in nearly every chunk score ~0; below this counts as no match

WORD = re.compile(r"\w+")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # Markdown link: keep the text, drop the URL
STOPWORDS = frozenset(
    """a about an and are as at be by can do does for from get how i in is it me my of on or
    the there this to we what when where which who why will with you your""".split()
)


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float  # higher is more relevant; only comparable within one retriever


class Retriever(Protocol):
    def search(self, question: str, k: int = 5) -> list[ScoredChunk]:
        """The top k chunks for the question, best first. Empty when nothing is relevant."""
        ...


class LocalKeywordRetriever:
    """BM25 keyword search over chunks in memory, using SQLite FTS5 from the standard library.

    title, heading_path, and text are indexed as separate columns (headings are not in text).
    The porter tokenizer lowercases, drops punctuation, and stems, so "Rooms" matches "room".
    """

    def __init__(self, chunks: list[Chunk], min_score: float = MIN_SCORE):
        self.chunks = chunks
        self.min_score = min_score
        self._lock = threading.Lock()  # /chat runs searches from several threads
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5(title, heading_path, text,"
            " tokenize='porter unicode61 remove_diacritics 2')"
        )
        self._db.executemany(
            "INSERT INTO chunks (rowid, title, heading_path, text) VALUES (?, ?, ?, ?)",
            [(i, c.title, c.heading_path, LINK.sub(r"\1", c.text)) for i, c in enumerate(chunks)],
        )

    @classmethod
    def from_dir(cls, chunks_dir: Path | str = CHUNKS_DIR, min_score: float = MIN_SCORE):
        """Load every chunk file ingestion.chunk wrote to chunks_dir."""
        chunks = [
            Chunk(**c)
            for path in sorted(Path(chunks_dir).glob("*.json"))
            for c in json.loads(path.read_text())["chunks"]
        ]
        return cls(chunks, min_score)

    def search(self, question: str, k: int = 5) -> list[ScoredChunk]:
        match = _match_query(question)
        if not match or k <= 0:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT rowid, rank FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?",
                (match, k * 4),  # extra rows so the low-text penalty can reorder them
            ).fetchall()
        results = []
        for rowid, rank in rows:
            chunk = self.chunks[rowid]
            score = -rank * (LOW_TEXT_PENALTY if chunk.low_text else 1.0)  # FTS5: lower is better
            if score > self.min_score:
                results.append(ScoredChunk(chunk, round(score, 4)))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:k]


def _match_query(question: str) -> str:
    """An FTS5 query that matches any content word of the question. User text is never parsed
    as FTS5 syntax: each word is quoted."""
    words = dict.fromkeys(w for w in WORD.findall(question.lower()) if w not in STOPWORDS)
    return " OR ".join(f'"{w}"' for w in words)


def get_retriever(config: Settings = settings) -> Retriever:
    """The retriever the RETRIEVER setting selects."""
    if config.retriever == "local":
        return LocalKeywordRetriever.from_dir(config.chunks_dir, config.retriever_min_score)
    if config.retriever == "azure":
        raise NotImplementedError("Azure AI Search retriever is not built yet (#22)")
    raise ValueError(f"Unknown RETRIEVER {config.retriever!r}: expected 'local' or 'azure'")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print the chunks retrieved for a question.")
    parser.add_argument("question")
    parser.add_argument("-k", type=int, default=5, help="number of results")
    args = parser.parse_args(argv)

    results = get_retriever().search(args.question, args.k)
    if not results:
        print("No relevant chunks found.")
    for rank, r in enumerate(results, 1):
        low = " [low text]" if r.chunk.low_text else ""
        print(f"{rank}. {r.score:.3f}  {r.chunk.title}{low}")
        print(f"   {r.chunk.heading_path}")
        print(f"   {r.chunk.source_url}")


if __name__ == "__main__":
    main()
