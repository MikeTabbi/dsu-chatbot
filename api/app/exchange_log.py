"""Store every /chat exchange and the feedback on it. /chat and /feedback depend on the ExchangeLog
interface only. Callers redact (api/app/redact.py) before anything reaches a store.

Where exchanges live in production is not decided yet; for now SqliteExchangeLog keeps them in a
local file under data/ (gitignored).
"""

import argparse
import json
import logging
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from api.app.config import Settings, settings

log = logging.getLogger(__name__)

Outcome = Literal["answered", "no_sources", "error"]
Rating = Literal["up", "down"]


@dataclass
class Exchange:
    request_id: str
    timestamp: str  # ISO 8601, UTC, e.g. 2026-10-02T15:04:05+00:00
    question: str  # redacted
    answer: str  # redacted; the text the student saw
    outcome: Outcome
    prompt_version: str  # prompt.prompt_version(): a hash of prompts/system.md
    latency_ms: int
    source_urls: list[str] = field(default_factory=list)  # the cited pages, as /chat returned them
    chunk_ids: list[str] = field(default_factory=list)  # every chunk retrieved, best first
    model: str | None = None  # None when Claude wasn't called or failed
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class Reviewed:
    """An exchange with the feedback on it."""

    exchange: Exchange
    rating: Rating | None
    comment: str | None  # redacted
    rated_at: str | None


class UnknownRequestError(LookupError):
    """No exchange has this request ID (never logged, or deleted by retention)."""


class ExchangeLog(Protocol):
    def add(self, exchange: Exchange) -> None:
        """Store one exchange."""
        ...

    def set_feedback(self, request_id: str, rating: Rating, comment: str | None) -> None:
        """Rate an exchange; rating it again replaces the earlier rating and comment.
        Raises UnknownRequestError when there is no exchange with this request ID."""
        ...

    def delete_older_than(self, cutoff: datetime) -> int:
        """Delete exchanges (and their feedback) from before cutoff. Returns how many."""
        ...

    def recent(
        self, rating: Rating | None = None, outcome: Outcome | None = None, limit: int = 20
    ) -> list[Reviewed]:
        """The newest exchanges first, optionally only those with this rating or outcome."""
        ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    request_id TEXT PRIMARY KEY,
    timestamp TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    outcome TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    latency_ms INTEGER NOT NULL,
    source_urls TEXT NOT NULL,  -- JSON list
    chunk_ids TEXT NOT NULL,  -- JSON list
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER
);
CREATE INDEX IF NOT EXISTS exchanges_timestamp ON exchanges (timestamp);
CREATE TABLE IF NOT EXISTS feedback (
    request_id TEXT PRIMARY KEY REFERENCES exchanges (request_id) ON DELETE CASCADE,
    rating TEXT NOT NULL CHECK (rating IN ('up', 'down')),
    comment TEXT,
    rated_at TEXT NOT NULL
);
"""

COLUMNS = (
    "request_id, timestamp, question, answer, outcome, prompt_version, latency_ms, source_urls,"
    " chunk_ids, model, input_tokens, output_tokens"
)


class SqliteExchangeLog:
    """Exchanges in one SQLite file, with feedback in a second table (one rating per exchange).

    Nothing touches the disk until the first call, so a bad path fails that call (which /chat
    logs as a warning) rather than startup. Each call opens its own connection, so the threads
    /chat runs on never share one.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        if not self._ready:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(self.path)) as db:
                    db.executescript(SCHEMA)
                self._ready = True
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA foreign_keys = ON")  # so deleting an exchange deletes its feedback
        return db

    def add(self, exchange: Exchange) -> None:
        e = exchange
        with closing(self._connect()) as db, db:
            db.execute(
                f"INSERT INTO exchanges ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    e.request_id,
                    e.timestamp,
                    e.question,
                    e.answer,
                    e.outcome,
                    e.prompt_version,
                    e.latency_ms,
                    json.dumps(e.source_urls),
                    json.dumps(e.chunk_ids),
                    e.model,
                    e.input_tokens,
                    e.output_tokens,
                ),
            )

    def set_feedback(self, request_id: str, rating: Rating, comment: str | None) -> None:
        with closing(self._connect()) as db, db:
            if not db.execute(
                "SELECT 1 FROM exchanges WHERE request_id = ?", (request_id,)
            ).fetchone():
                raise UnknownRequestError(request_id)
            db.execute(
                "INSERT INTO feedback (request_id, rating, comment, rated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (request_id) DO UPDATE SET rating = excluded.rating,"
                " comment = excluded.comment, rated_at = excluded.rated_at",
                (request_id, rating, comment, now()),
            )

    def delete_older_than(self, cutoff: datetime) -> int:
        with closing(self._connect()) as db, db:
            return db.execute("DELETE FROM exchanges WHERE timestamp < ?", (_iso(cutoff),)).rowcount

    def recent(
        self, rating: Rating | None = None, outcome: Outcome | None = None, limit: int = 20
    ) -> list[Reviewed]:
        where, params = [], []
        if rating:
            where.append("f.rating = ?")
            params.append(rating)
        if outcome:
            where.append("e.outcome = ?")
            params.append(outcome)
        columns = ", ".join(f"e.{c.strip()}" for c in COLUMNS.split(","))
        sql = (
            f"SELECT {columns}, f.rating, f.comment, f.rated_at FROM exchanges e"
            " LEFT JOIN feedback f USING (request_id)"
            + (f" WHERE {' AND '.join(where)}" if where else "")
            + " ORDER BY e.timestamp DESC, e.rowid DESC LIMIT ?"
        )
        with closing(self._connect()) as db:
            rows = db.execute(sql, (*params, limit)).fetchall()
        return [_reviewed(row) for row in rows]


def _reviewed(row: tuple) -> Reviewed:
    """COLUMNS are in Exchange's field order; the feedback columns follow."""
    values = list(row[:12])
    values[7], values[8] = json.loads(values[7]), json.loads(values[8])  # source_urls, chunk_ids
    return Reviewed(Exchange(*values), *row[12:])


class NullExchangeLog:
    """Keeps nothing. For EXCHANGE_LOG=none and eval runs, whose questions aren't students'."""

    def add(self, exchange: Exchange) -> None:
        pass

    def set_feedback(self, request_id: str, rating: Rating, comment: str | None) -> None:
        raise UnknownRequestError(request_id)

    def delete_older_than(self, cutoff: datetime) -> int:
        return 0

    def recent(
        self, rating: Rating | None = None, outcome: Outcome | None = None, limit: int = 20
    ) -> list[Reviewed]:
        return []


def now() -> str:
    return _iso(datetime.now(UTC))


def _iso(moment: datetime) -> str:
    """UTC, to the second, so stored timestamps sort and compare as text."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def get_exchange_log(config: Settings = settings) -> ExchangeLog:
    """The store the EXCHANGE_LOG setting selects."""
    if config.exchange_log == "sqlite":
        return SqliteExchangeLog(config.exchange_log_path)
    if config.exchange_log == "none":
        return NullExchangeLog()
    raise ValueError(f"Unknown EXCHANGE_LOG {config.exchange_log!r}: expected 'sqlite' or 'none'")


def print_reviewed(items: list[Reviewed]) -> None:
    for item in items:
        e = item.exchange
        print(f"=== {e.timestamp}  {e.request_id}  outcome={e.outcome}  prompt={e.prompt_version}")
        if item.rating:
            print(f"Rating:   {item.rating}" + (f" ({item.rated_at})" if item.rated_at else ""))
        print(f"Question: {e.question}")
        print("Answer:")
        for line in e.answer.splitlines():
            print(f"  {line}")
        print("Sources:  " + (", ".join(e.source_urls) or "none"))
        if item.rating:
            print(f"Comment:  {item.comment or '(none)'}")
        print()


def main(argv: list[str] | None = None, config: Settings = settings) -> None:
    parser = argparse.ArgumentParser(description="Review and clean up logged /chat exchanges.")
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="recent thumbs-down exchanges, newest first")
    review.add_argument("-n", "--limit", type=int, default=20)
    unanswered = commands.add_parser(
        "unanswered", help="recent questions retrieval found no sources for (missing content)"
    )
    unanswered.add_argument("-n", "--limit", type=int, default=20)
    purge = commands.add_parser("purge", help="delete exchanges older than the retention period")
    purge.add_argument(
        "--days",
        type=int,
        default=config.exchange_retention_days,
        help="days to keep (default: the EXCHANGE_RETENTION_DAYS setting)",
    )
    args = parser.parse_args(argv)

    store = get_exchange_log(config)
    if args.command == "review":
        items = store.recent(rating="down", limit=args.limit)
        print(f"{len(items)} thumbs-down exchange(s), newest first\n")
        print_reviewed(items)
    elif args.command == "unanswered":
        items = store.recent(outcome="no_sources", limit=args.limit)
        print(f"{len(items)} question(s) with no sources found, newest first\n")
        print_reviewed(items)
    else:
        if args.days < 0:
            parser.error("--days can't be negative")
        cutoff = datetime.now(UTC) - timedelta(days=args.days)
        deleted = store.delete_older_than(cutoff)
        print(f"Deleted {deleted} exchange(s) from before {_iso(cutoff)} (kept {args.days} days)")


if __name__ == "__main__":
    main()
