"""Bring the chunks and the search index up to date with sources.yaml and desu.edu.

Each run:
1. deletes the files of pages that are no longer in sources.yaml;
2. checks the due pages: crawl, extract, then chunk. Pages the server says are unchanged (304),
   and pages whose extracted text and modified time are the same as last time, are not re-chunked;
3. re-processes saved pages processed by an older extractor or chunker version, due or not;
4. deletes the files of a page that answered 404 or 410 on several checks in a row;
5. syncs the search index with the chunk files, by chunk ID.

Each page's state is its crawler metadata, data/raw/<page>.json: the crawler's validators and
check times, plus the pipeline's missing_checks, extractor_version and chunker_version.
"""

import argparse
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from ingestion import chunk, extract
from ingestion.crawler import DEFAULT_DELAY, Crawler, FetchResult, _file_stem
from ingestion.crawler import DEFAULT_OUTPUT_DIR as DEFAULT_RAW_DIR
from ingestion.index import DEFAULT_PATH as DEFAULT_INDEX_PATH
from ingestion.index import Index, LocalIndex, SyncResult, load_chunk_files, sync
from ingestion.sources import (
    ALLOWED_DOMAIN,
    Source,
    load_check_intervals,
    load_delete_after,
    load_sources,
)
from ingestion.sources import DEFAULT_PATH as DEFAULT_SOURCES_PATH

# A page is due a little before its interval is up, so a daily job that happens to reach a page a
# few seconds earlier than it did yesterday still checks it.
DUE_SLACK = timedelta(hours=1)
GONE_STATUSES = (404, 410)
PAGE_FILE = re.compile(r"^(?P<stem>.+-[0-9a-f]{8})\.(html|json)$")  # named by crawler._file_stem

log = logging.getLogger(__name__)


@dataclass
class Summary:
    """Source URLs by what happened to them this run, and what changed in the index."""

    unchanged: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    reprocessed: list[str] = field(default_factory=list)  # same page, newer extractor/chunker
    failed: list[str] = field(default_factory=list)  # "url: reason"
    not_due: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # "url: reason"
    redirects: list[str] = field(default_factory=list)  # "url -> where it permanently moved"
    checked: int = 0
    index: SyncResult = field(default_factory=SyncResult)

    def __str__(self) -> str:
        lines = [
            f"Checked {self.checked}: {len(self.unchanged)} unchanged, "
            f"{len(self.changed)} changed, {len(self.new)} new, {len(self.failed)} failed. "
            f"Skipped {len(self.not_due)} not due. "
            f"Reprocessed {len(self.reprocessed)} for a new extractor or chunker version.",
            f"Chunks: {len(self.index.added)} added, {len(self.index.updated)} updated, "
            f"{len(self.index.removed)} removed.",
            f"Pages deleted: {len(self.deleted)}. Pages failing: {len(self.failed)}. "
            f"Redirects to review: {len(self.redirects)}.",
        ]
        lines += [f"  deleted: {d}" for d in self.deleted]
        lines += [f"  failing: {f}" for f in self.failed]
        lines += [f"  redirect: {r} (update sources.yaml)" for r in self.redirects]
        return "\n".join(lines)


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
    index: Index,
    registered: Iterable[str] | None = None,
    intervals: dict[str, timedelta] | None = None,
    delete_after: int | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> Summary:
    """Check the due sources (all of them with force), re-chunk the ones that changed, and sync
    the index. registered is every URL in sources.yaml (default: the URLs of sources); pages
    outside it are deleted, so pass it whenever sources is only part of the registry."""
    sources = list(sources)
    dirs = _Dirs(crawler.output_dir, Path(extracted_dir), Path(chunks_dir))
    intervals = intervals or load_check_intervals()
    delete_after = delete_after or load_delete_after()
    now = now or datetime.now(UTC)
    summary = Summary()

    registered = set(registered) if registered is not None else {s.url for s in sources}
    summary.deleted += _prune(registered, dirs)

    due, outdated = [], []
    for source in sources:
        meta = crawler.load_meta(source.url)
        if force or is_due(meta, source.change_frequency, intervals, now):
            due.append(source)
        elif meta and meta.get("html_file") and not _versions_current(meta):
            outdated.append(source)
        else:
            summary.not_due.append(source.url)

    for result in crawler.crawl(due):
        summary.checked += 1
        if result.moved_to and _on_domain(result.moved_to):
            summary.redirects.append(f"{result.url} -> {result.moved_to}")
        if result.ok:
            _process_page(result.url, crawler, dirs, summary, not_modified=result.not_modified)
        else:
            summary.failed.append(_record_failure(result, crawler, dirs, delete_after, summary))

    for source in outdated:  # the saved copy is still current; only the processing changed
        _process_page(source.url, crawler, dirs, summary, not_modified=True)

    summary.index = sync(index, load_chunk_files(dirs.chunks))
    return summary


@dataclass(frozen=True)
class _Dirs:
    raw: Path
    extracted: Path
    chunks: Path

    def page_files(self, stem: str, meta: bool) -> list[Path]:
        """The page's raw HTML, extracted page, and chunks, plus its crawler metadata if meta."""
        files = [self.raw / f"{stem}.html", self.extracted / f"{stem}.json"]
        files.append(self.chunks / f"{stem}.json")
        return [*files, self.raw / f"{stem}.json"] if meta else files


def _process_page(
    url: str, crawler: Crawler, dirs: _Dirs, summary: Summary, *, not_modified: bool
) -> None:
    meta = crawler.load_meta(url) or {}
    if meta.get("missing_checks"):
        crawler.update_meta(url, missing_checks=0)  # the page answered again
    try:
        outcome = _process(meta, dirs, not_modified)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("could not process %s: %r", url, e)
        summary.failed.append(f"{url}: {e!r}")
        return
    crawler.update_meta(
        url, extractor_version=extract.EXTRACTOR_VERSION, chunker_version=chunk.CHUNKER_VERSION
    )
    log.info("%s %s", outcome, url)
    getattr(summary, outcome).append(url)


def _process(meta: dict, dirs: _Dirs, not_modified: bool) -> str:
    """Extract and chunk one saved page if it or the processing code changed. Returns the
    summary bucket it goes in."""
    name = Path(meta["html_file"]).with_suffix(".json").name
    previous = _read_json(dirs.extracted / name)
    chunked = (dirs.chunks / name).exists()
    current = _versions_current(meta)

    if not_modified and previous is not None and chunked and current:
        return "unchanged"
    page = extract.extract_page(dirs.raw / name, dirs.extracted)
    same = previous is not None and _same_content(previous, page)
    if not (same and chunked and current):
        chunk.chunk_file(dirs.extracted / name, dirs.chunks)
    if previous is None:
        return "new"
    if not same:
        return "changed"
    return "unchanged" if current else "reprocessed"


def _versions_current(meta: dict) -> bool:
    return (
        meta.get("extractor_version") == extract.EXTRACTOR_VERSION
        and meta.get("chunker_version") == chunk.CHUNKER_VERSION
    )


def _record_failure(
    result: FetchResult, crawler: Crawler, dirs: _Dirs, delete_after: int, summary: Summary
) -> str:
    """Count a 404/410 toward deleting the page, deleting it once there are enough in a row.
    Other failures (timeouts, 5xx, robots.txt) don't count. Returns the summary line."""
    if result.status not in GONE_STATUSES:
        return f"{result.url}: {result.error}"
    misses = (crawler.load_meta(result.url) or {}).get("missing_checks", 0) + 1
    if misses < delete_after:
        crawler.update_meta(result.url, missing_checks=misses)
        return (
            f"{result.url}: {result.error} on {misses} check(s) in a row "
            f"(deleted after {delete_after})"
        )
    # Keep the metadata, without its html_file, so the count carries on and the page is
    # re-fetched in full if it comes back.
    if _remove(dirs.page_files(_file_stem(result.url), meta=False)):
        summary.deleted.append(f"{result.url}: {result.error} on {misses} checks in a row")
    crawler.update_meta(result.url, missing_checks=misses, html_file=None)
    return f"{result.url}: {result.error} on {misses} checks in a row (deleted)"


def _prune(registered: set[str], dirs: _Dirs) -> list[str]:
    """Delete the files of every page not in the registry. Returns a summary line for each."""
    if not registered:
        log.warning("no sources registered; not deleting any pages")
        return []
    keep = {_file_stem(url) for url in registered}
    gone: dict[str, str] = {}  # stem -> url
    for directory in (dirs.raw, dirs.extracted, dirs.chunks):
        for path in sorted(directory.glob("*")):
            match = PAGE_FILE.match(path.name)
            if match and match["stem"] not in keep and match["stem"] not in gone:
                url = (_read_json(path.with_suffix(".json")) or {}).get("url")
                gone[match["stem"]] = url or match["stem"]
    for stem in gone:
        _remove(dirs.page_files(stem, meta=True))
    return [f"{url}: no longer in sources.yaml" for url in gone.values()]


def _remove(paths: list[Path]) -> bool:
    """Delete the files that exist. Returns whether there were any."""
    existing = [p for p in paths if p.exists()]
    for path in existing:
        log.info("deleting %s", path)
        path.unlink()
    return bool(existing)


def _on_domain(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)


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
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--force", action="store_true", help="check every source, due or not")
    parser.add_argument("--limit", type=int, help="only check the first N sources")
    parser.add_argument("--topic", help="only check sources with this topic")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, help="minimum seconds between requests"
    )
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_PATH)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--extracted", type=Path, default=extract.DEFAULT_OUTPUT_DIR)
    parser.add_argument("--chunks", type=Path, default=chunk.DEFAULT_OUTPUT_DIR)
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX_PATH, help="local index file")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    registry = load_sources(args.sources)
    sources = [s for s in registry if args.topic in (None, s.topic)][: args.limit]
    summary = run(
        sources,
        Crawler(args.raw, delay=args.delay),
        args.extracted,
        args.chunks,
        index=LocalIndex(args.index),
        registered=[s.url for s in registry],  # --topic and --limit never delete other pages
        intervals=load_check_intervals(args.sources),
        delete_after=load_delete_after(args.sources),
        force=args.force,
    )
    print(summary)
    if summary.failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
