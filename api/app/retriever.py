"""Find the chunks most relevant to a question. /chat depends on the Retriever interface only."""

import argparse
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from api.app.config import Settings, settings
from ingestion.chunk import Chunk
from ingestion.index import LocalIndex

log = logging.getLogger(__name__)

SYNONYMS_PATH = Path(__file__).resolve().parent / "synonyms.yaml"
SYNONYM_WEIGHT = 0.5  # a synonym match counts half as much as the student's own word
LOW_TEXT_PENALTY = 0.8  # low-text chunks keep 80% of their score, so they lose close calls
MIN_SCORE = 0.1  # words found in nearly every chunk score ~0; below this counts as no match

WORD = re.compile(r"\w+")
LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")  # Markdown link: keep the text, drop the URL
STOPWORDS = frozenset(
    """a about all an and are as at be by can do does for from get has have how i in is it me my
    of on or the there this to we what when where which who why will with y you your""".split()
)  # "y" and "all" come from "y'all"


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float  # higher is more relevant; only comparable within one retriever


class Retriever(Protocol):
    def search(self, question: str, k: int = 5) -> list[ScoredChunk]:
        """The top k chunks for the question, best first. Empty when nothing is relevant."""
        ...


class SynonymsError(ValueError):
    """The synonyms file is not in the expected format."""


def load_synonyms(path: Path | str = SYNONYMS_PATH) -> list[list[str]]:
    """The word groups in a synonyms file (see synonyms.yaml). A missing or empty file means
    no synonyms. Raises SynonymsError, naming the group, when the format is wrong."""
    path = Path(path)
    if not path.exists():
        log.warning("synonyms file %s not found; searching without synonyms", path)
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SynonymsError(f"{path}: not valid YAML: {e}") from e
    if data is None:
        return []
    if not isinstance(data, dict) or set(data) != {"groups"}:
        raise SynonymsError(f"{path}: expected one top-level key, 'groups:', with a list below it")
    if data["groups"] is None:
        return []
    if not isinstance(data["groups"], list):
        raise SynonymsError(f"{path}: 'groups' must be a list of lines starting with '- '")

    groups, seen = [], {}
    for n, raw in enumerate(data["groups"], 1):
        where = f"{path}: group {n} ({raw!r})"
        if isinstance(raw, str):
            terms = raw.split(",")
        elif isinstance(raw, list) and all(isinstance(t, str) for t in raw):
            terms = raw
        else:
            raise SynonymsError(f"{where}: write the group as words separated by commas")
        terms = [" ".join(WORD.findall(t.lower())) for t in terms]
        if any(not t for t in terms):
            raise SynonymsError(f"{where}: has an empty entry (check for extra commas)")
        if len(set(terms)) < 2:
            raise SynonymsError(f"{where}: needs at least two different words or phrases")
        for t in terms:
            key = _fold_phrase(t)
            if key in seen and seen[key] != n:
                raise SynonymsError(f"{where}: {t!r} is already in group {seen[key]}")
            seen[key] = n
        groups.append(list(dict.fromkeys(terms)))
    return groups


class LocalKeywordRetriever:
    """BM25 keyword search over chunks in memory, using SQLite FTS5 from the standard library.

    title, heading_path, and text are indexed as separate columns (headings are not in text).
    The porter tokenizer lowercases, drops punctuation, and stems, so "Rooms" matches "room".

    Synonyms only change the query, never the index: words from a group the question mentions
    are searched separately, and their score is weighted down by synonym_weight before it is
    added to the score of the student's own words.
    """

    def __init__(
        self,
        chunks: list[Chunk],
        min_score: float = MIN_SCORE,
        synonyms: list[list[str]] | None = None,
        synonym_weight: float = SYNONYM_WEIGHT,
    ):
        self.chunks = chunks
        self.min_score = min_score
        self.synonyms = synonyms or []
        self.synonym_weight = synonym_weight
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

    def expand(self, question: str) -> list[str]:
        """The synonyms added to the question: the other words of every group it mentions."""
        asked = _fold_phrase(question)
        added = []
        for group in self.synonyms:
            if any(_contains_phrase(asked, _fold_phrase(t)) for t in group):
                added += [t for t in group if not _contains_phrase(asked, _fold_phrase(t))]
        return list(dict.fromkeys(added))

    def search(self, question: str, k: int = 5) -> list[ScoredChunk]:
        match = _match_query(question)
        if not match or k <= 0:
            return []
        added = self.expand(question)
        scores = self._bm25(match)
        if added:
            synonym_match = " OR ".join(f'"{t}"' for t in added)
            for rowid, score in self._bm25(synonym_match).items():
                scores[rowid] = scores.get(rowid, 0.0) + self.synonym_weight * score
        results = []
        for rowid, score in scores.items():
            chunk = self.chunks[rowid]
            score *= LOW_TEXT_PENALTY if chunk.low_text else 1.0
            if score > self.min_score:
                results.append(ScoredChunk(chunk, round(score, 4)))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:k]

    def _bm25(self, match: str) -> dict[int, float]:
        """BM25 score by rowid for every chunk the FTS5 query matches, higher is better. No
        LIMIT: a chunk outside one query's top rows can still win once both scores are added,
        and the low-text penalty can reorder them. A few hundred chunks score in about 1 ms."""
        with self._lock:
            rows = self._db.execute(
                "SELECT rowid, rank FROM chunks WHERE chunks MATCH ?", (match,)
            ).fetchall()
        return {rowid: -rank for rowid, rank in rows}  # FTS5: lower rank is better


class ReloadingRetriever:
    """The local retriever over the index file the ingestion pipeline syncs, rebuilt whenever
    that file changes, so a running server picks up new chunks without a restart.

    Each search stats the file (microseconds) and rebuilds only when it was replaced or its
    modification time or size changed. The pipeline replaces the file atomically, so a rebuild
    never sees half a file. If a rebuild fails, the previous chunks keep serving until the file
    changes again.
    """

    def __init__(
        self,
        index_path: Path | str,
        min_score: float = MIN_SCORE,
        synonyms: list[list[str]] | None = None,
    ):
        self.index = LocalIndex(index_path)
        self.min_score = min_score
        self.synonyms = synonyms
        self._lock = threading.Lock()
        self._version: tuple[int, int, int] | None | str = "not loaded"
        self._current = LocalKeywordRetriever([], min_score, synonyms)
        self._reload_if_changed()

    @property
    def chunks(self) -> list[Chunk]:
        return self._reload_if_changed().chunks

    def expand(self, question: str) -> list[str]:
        return self._current.expand(question)

    def search(self, question: str, k: int = 5) -> list[ScoredChunk]:
        return self._reload_if_changed().search(question, k)

    def _reload_if_changed(self) -> LocalKeywordRetriever:
        version = self._file_version()
        if version == self._version:
            return self._current
        with self._lock:
            if version != self._version:
                try:
                    chunks = self.index.chunks()
                except (OSError, ValueError, KeyError, TypeError) as e:
                    log.error(
                        "could not load %s; keeping the previous chunks: %r", self.index.path, e
                    )
                    self._version = version  # retry once the file changes again
                    return self._current
                self._current = LocalKeywordRetriever(chunks, self.min_score, self.synonyms)
                self._version = version
                if version is None:
                    log.warning("no index at %s; run python -m ingestion.pipeline", self.index.path)
                else:
                    log.info("loaded %d chunks from %s", len(chunks), self.index.path)
        return self._current

    def _file_version(self) -> tuple[int, int, int] | None:
        try:
            stat = self.index.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_ino, stat.st_mtime_ns, stat.st_size


def _match_query(question: str) -> str:
    """An FTS5 query that matches any content word of the question. User text is never parsed
    as FTS5 syntax: each word is quoted."""
    words = dict.fromkeys(w for w in WORD.findall(question.lower()) if w not in STOPWORDS)
    return " OR ".join(f'"{w}"' for w in words)


def _fold(word: str) -> str:
    """Plural to singular, roughly, so "dorms" matches the synonym "dorm"."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _fold_phrase(text: str) -> str:
    return " ".join(_fold(w) for w in WORD.findall(text.lower()))


def _contains_phrase(text: str, phrase: str) -> bool:
    """Whole words only: "hall" is in "residence hall", not in "challenge"."""
    return f" {phrase} " in f" {text} "


def get_retriever(config: Settings = settings) -> Retriever:
    """The retriever the RETRIEVER setting selects."""
    if config.retriever == "local":
        return ReloadingRetriever(
            config.index_path, config.retriever_min_score, load_synonyms(config.synonyms_file)
        )
    if config.retriever == "azure":
        raise NotImplementedError("Azure AI Search retriever is not built yet (#22)")
    raise ValueError(f"Unknown RETRIEVER {config.retriever!r}: expected 'local' or 'azure'")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Print the chunks retrieved for a question.")
    parser.add_argument("question")
    parser.add_argument("-k", type=int, default=5, help="number of results")
    args = parser.parse_args(argv)

    retriever = get_retriever()
    added = retriever.expand(args.question) if hasattr(retriever, "expand") else []
    if added:
        print(f"Synonyms added: {', '.join(added)}")
    results = retriever.search(args.question, args.k)
    if not results:
        print("No relevant chunks found.")
    for rank, r in enumerate(results, 1):
        low = " [low text]" if r.chunk.low_text else ""
        print(f"{rank}. {r.score:.3f}  {r.chunk.title}{low}")
        print(f"   {r.chunk.heading_path}")
        print(f"   {r.chunk.source_url}")


if __name__ == "__main__":
    main()
