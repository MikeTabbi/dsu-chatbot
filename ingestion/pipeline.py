"""Bring the chunks up to date for every source that is due: crawl, extract, then chunk.

Pages the server says are unchanged (304), and pages whose extracted text and modified time are
the same as last time, are not re-chunked.
"""

import argparse
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ingestion import chunk, extract
from ingestion.crawler import DEFAULT_DELAY, Crawler, FetchResult
from ingestion.crawler import DEFAULT_OUTPUT_DIR as DEFAULT_RAW_DIR
from ingestion.sources import Source, load_check_intervals, load_sources

# A page is due a little before its interval is up, so a daily job that happens to reach a page a
# few seconds earlier than it did yesterday still checks it.
DUE_SLACK = timedelta(hours=1)

log = logging.getLogger(__name__)


@dataclass
class Summary:
    """Source URLs by what happened to them this run."""

    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "url: reason"
    not_due: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.unchanged) + len(self.changed) + len(self.new) + len(self.failed)

    def __str__(self) -> str:
        return (
            f"Checked {self.checked}: {len(self.unchanged)} unchanged, "
            f"{len(self.changed)} changed, {len(self.new)} new, {len(self.failed)} failed. "
            f"Skipped {len(self.not_due)} not due."
        )


def is_due(
    meta: dict | None, frequency: str, intervals: dict[str, timedelta], now: datetime
) -> bool:
    """Whether a page last checked as recorded in its crawler metadata should be checked now."""
    last = (meta or {}).get("checked_at") or (meta or {}).get("fetched_at")
    try:
        last_checked = datetime.fromisoformat(last)
    except (TypeError, ValueError):
        return True  # never fetched, or no usable time on record
    return now - last_checked >= intervals[frequency] - DUE_SLACK


def run(
    sources: Iterable[Source],
    crawler: Crawler,
    extracted_dir: Path | str = extract.DEFAULT_OUTPUT_DIR,
    chunks_dir: Path | str = chunk.DEFAULT_OUTPUT_DIR,
    *,
    intervals: dict[str, timedelta] | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> Summary:
    """Check the due sources (all of them with force) and re-chunk the ones that changed."""
    extracted_dir, chunks_dir = Path(extracted_dir), Path(chunks_dir)
    intervals = intervals or load_check_intervals()
    now = now or datetime.now(UTC)
    summary = Summary()

    due = []
    for source in sources:
        if force or is_due(crawler.load_meta(source.url), source.change_frequency, intervals, now):
            due.append(source)
        else:
            summary.not_due.append(source.url)

    for result in crawler.crawl(due):
        if not result.ok:
            summary.failed.append(f"{result.url}: {result.error}")
            continue
        try:
            outcome = _process(result, crawler.output_dir, extracted_dir, chunks_dir)
        except (OSError, ValueError, KeyError) as e:
            log.warning("could not process %s: %r", result.url, e)
            summary.failed.append(f"{result.url}: {e!r}")
            continue
        log.info("%s %s", outcome, result.url)
        getattr(summary, outcome).append(result.url)
    return summary


def _process(result: FetchResult, raw_dir: Path, extracted_dir: Path, chunks_dir: Path) -> str:
    """Extract and chunk one fetched page if it changed. Returns the summary bucket it goes in."""
    name = Path(result.html_file).with_suffix(".json").name
    previous = _read_json(extracted_dir / name)
    chunked = (chunks_dir / name).exists()

    if result.not_modified and previous is not None and chunked:
        return "unchanged"
    page = extract.extract_page(raw_dir / name, extracted_dir)
    same = previous is not None and _same_content(previous, page)
    if not (same and chunked):
        chunk.chunk_file(extracted_dir / name, chunks_dir)
    if previous is None:
        return "new"
    return "unchanged" if same else "changed"


def _same_content(previous: dict, page: extract.ExtractedPage) -> bool:
    # article:modified_time alone can't show a page is unchanged (some edits don't update it),
    # so the text must match too. A new modified_time re-chunks even when the text is the same,
    # so the chunks carry the current date.
    old_hash = previous.get("text_hash") or extract.text_hash(previous.get("text", ""))
    return old_hash == page.text_hash and previous.get("modified_time") == page.modified_time


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="check every source, due or not")
    parser.add_argument("--limit", type=int, help="only consider the first N sources")
    parser.add_argument("--topic", help="only consider sources with this topic")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, help="minimum seconds between requests"
    )
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--extracted", type=Path, default=extract.DEFAULT_OUTPUT_DIR)
    parser.add_argument("--chunks", type=Path, default=chunk.DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sources = [s for s in load_sources() if args.topic in (None, s.topic)][: args.limit]
    summary = run(
        sources,
        Crawler(args.raw, delay=args.delay),
        args.extracted,
        args.chunks,
        force=args.force,
    )
    print(summary)
    for failure in summary.failed:
        print(f"  failed: {failure}")


if __name__ == "__main__":
    main()
