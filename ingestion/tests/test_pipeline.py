import json
from datetime import datetime, timedelta

import httpx
import pytest

from ingestion.pipeline import DUE_SLACK, Summary, is_due, run
from ingestion.sources import Source, load_check_intervals
from ingestion.tests.test_crawler import SAMPLE_PAGE, SITE, FakeSite, html, make_crawler

ETAG = '"1790963877-1"'
LAST_MODIFIED = "Fri, 02 Oct 2026 17:57:57 GMT"
INTERVALS = {"fast": timedelta(0), "medium": timedelta(days=1), "slow": timedelta(days=7)}

# Same page, but only the site chrome differs: the extracted text is identical.
CHROME_ONLY_EDIT = SAMPLE_PAGE.replace(b"<footer>Delaware", b"<footer>Dover, Delaware")
TEXT_EDIT = SAMPLE_PAGE.replace(b"Sample fixture", b"Updated fixture")  # modified_time not bumped
DATE_EDIT = SAMPLE_PAGE.replace(b"2026-08-14T10:22:31", b"2026-09-01T09:00:00")


def source(path, change_frequency="medium"):
    return Source(url=SITE + path, topic="housing", change_frequency=change_frequency, notes="t")


def with_validators(body=SAMPLE_PAGE, etag=ETAG, last_modified=LAST_MODIFIED):
    """Answers like desu.edu's Drupal page cache: 304 only when both validators match."""

    def respond(request):
        if (
            request.headers.get("if-none-match") == etag
            and request.headers.get("if-modified-since") == last_modified
        ):
            return httpx.Response(304, headers={"etag": etag})
        headers = {"content-type": "text/html; charset=utf-8", "etag": etag}
        return httpx.Response(
            200, content=body, headers={**headers, "last-modified": last_modified}
        )

    return respond


class Pipeline:
    """Runs the pipeline on a fake site with state kept in tmp_path, like repeated cron runs."""

    def __init__(self, tmp_path):
        self.raw = tmp_path / "raw"
        self.extracted = tmp_path / "extracted"
        self.chunks = tmp_path / "chunks"

    def run(self, site, sources, now=None, force=False):
        # A fresh crawler every run: nothing carries over except what is on disk.
        crawler, self.clock = make_crawler(self.raw, site, delay=0.0)
        return run(
            sources,
            crawler,
            self.extracted,
            self.chunks,
            intervals=INTERVALS,
            force=force,
            now=now,
        )

    def raw_meta(self, path):
        [meta] = [
            json.loads(p.read_text())
            for p in self.raw.glob("*.json")
            if p.name != "manifest.json" and json.loads(p.read_text())["url"] == SITE + path
        ]
        return meta

    def chunk_file(self):
        [path] = self.chunks.glob("*.json")
        return path

    def mark_chunks(self):
        """Overwrite the chunk file with a marker, to tell whether a run rewrites it."""
        self.chunk_file().write_text('{"marker": true}')

    def chunks_rewritten(self):
        return "marker" not in json.loads(self.chunk_file().read_text())

    def last_checked(self, path):
        return datetime.fromisoformat(self.raw_meta(path)["checked_at"])


@pytest.fixture
def pipeline(tmp_path):
    return Pipeline(tmp_path)


def test_first_run_processes_new_pages_and_saves_state(pipeline):
    site = FakeSite({"/housing": with_validators()})

    summary = pipeline.run(site, [source("/housing")])

    assert summary.new == [SITE + "/housing"]
    assert summary.checked == 1
    meta = pipeline.raw_meta("/housing")
    assert (meta["etag"], meta["last_modified"]) == (ETAG, LAST_MODIFIED)
    assert meta["checked_at"] == meta["fetched_at"]
    [extracted] = pipeline.extracted.glob("*.json")
    page = json.loads(extracted.read_text())
    assert page["modified_time"] == "2026-08-14T10:22:31-04:00"
    assert len(page["text_hash"]) == 64
    assert json.loads(pipeline.chunk_file().read_text())["chunks"]


def test_304_skips_extract_and_chunk_and_records_the_check(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    first = pipeline.raw_meta("/housing")
    pipeline.mark_chunks()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.unchanged == [SITE + "/housing"]
    request = site.requests[-1]
    assert request.headers["if-none-match"] == ETAG
    assert request.headers["if-modified-since"] == LAST_MODIFIED
    assert not pipeline.chunks_rewritten()
    meta = pipeline.raw_meta("/housing")
    assert meta["status"] == 304
    assert meta["fetched_at"] == first["fetched_at"]  # the saved body is still the first one
    assert meta["checked_at"] >= first["checked_at"]
    assert meta["html_file"] == first["html_file"]


def test_crawl_delay_applies_to_requests_that_return_304(pipeline):
    site = FakeSite({"/a": with_validators(), "/b": with_validators()})
    sources = [source("/a"), source("/b")]
    pipeline.run(site, sources)

    pipeline.run(site, sources, force=True)

    # robots.txt, /a (304), /b (304): each request after the first waits out the 10s Crawl-delay.
    assert pipeline.clock.sleeps == [10.0, 10.0]
    assert [r.headers.get("if-none-match") for r in site.requests[-2:]] == [ETAG, ETAG]


def test_without_validators_an_identical_text_hash_skips_rechunking(pipeline):
    pages = iter([SAMPLE_PAGE, CHROME_ONLY_EDIT])
    site = FakeSite({"/housing": lambda request: html(next(pages))})
    pipeline.run(site, [source("/housing")])
    pipeline.mark_chunks()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert "if-none-match" not in site.requests[-1].headers
    assert summary.unchanged == [SITE + "/housing"]
    assert not pipeline.chunks_rewritten()


@pytest.mark.parametrize(
    "edit",
    [TEXT_EDIT, DATE_EDIT],
    ids=["text changed, same modified_time", "new modified_time, same text"],
)
def test_changed_text_or_modified_time_is_rechunked(pipeline, edit):
    pages = iter([SAMPLE_PAGE, edit])
    site = FakeSite({"/housing": lambda request: html(next(pages))})
    pipeline.run(site, [source("/housing")])
    pipeline.mark_chunks()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.changed == [SITE + "/housing"]
    assert pipeline.chunks_rewritten()


def test_changed_page_behind_validators_is_rechunked(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    site.routes["/housing"] = with_validators(TEXT_EDIT, etag='"1790999999-1"')

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.changed == [SITE + "/housing"]
    assert pipeline.raw_meta("/housing")["etag"] == '"1790999999-1"'
    assert "Updated fixture" in pipeline.chunk_file().read_text()


@pytest.mark.parametrize(
    ("elapsed", "checked"),
    [
        (timedelta(hours=2), ["fast"]),
        (timedelta(hours=23, minutes=30), ["fast", "medium"]),  # within DUE_SLACK of a day
        (timedelta(days=2), ["fast", "medium"]),
        (timedelta(days=7), ["fast", "medium", "slow"]),
    ],
)
def test_only_due_pages_are_checked(pipeline, elapsed, checked):
    site = FakeSite({f"/{f}": with_validators() for f in INTERVALS})
    sources = [source(f"/{f}", f) for f in INTERVALS]
    pipeline.run(site, sources)
    site.requests.clear()
    now = pipeline.last_checked("/slow") + elapsed

    summary = pipeline.run(site, sources, now=now)

    assert site.paths == ["/robots.txt"] + [f"/{f}" for f in checked]
    assert summary.unchanged == [SITE + f"/{f}" for f in checked]
    assert summary.not_due == [SITE + f"/{f}" for f in INTERVALS if f not in checked]


def test_nothing_due_makes_no_requests(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing", "slow")])
    site.requests.clear()

    summary = pipeline.run(site, [source("/housing", "slow")])

    assert site.requests == []
    assert (summary.checked, summary.not_due) == (0, [SITE + "/housing"])


def test_force_checks_pages_that_are_not_due(pipeline):
    site = FakeSite({"/medium": with_validators(), "/slow": with_validators()})
    sources = [source("/medium", "medium"), source("/slow", "slow")]
    pipeline.run(site, sources)
    site.requests.clear()

    summary = pipeline.run(site, sources, force=True)

    assert site.paths == ["/robots.txt", "/medium", "/slow"]
    assert (summary.checked, summary.not_due) == (2, [])


def test_failed_page_keeps_its_previous_state_and_stays_due(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    before = pipeline.raw_meta("/housing")
    site.routes["/housing"] = httpx.Response(404)

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert summary.failed == [SITE + "/housing: HTTP 404"]
    assert pipeline.raw_meta("/housing") == before
    assert json.loads(pipeline.chunk_file().read_text())["chunks"]


def test_new_source_alongside_unchanged_ones(pipeline):
    site = FakeSite({"/a": with_validators(), "/b": with_validators()})
    pipeline.run(site, [source("/a")])

    summary = pipeline.run(site, [source("/a"), source("/b")], force=True)

    assert (summary.unchanged, summary.new) == ([SITE + "/a"], [SITE + "/b"])


def test_304_for_a_page_never_chunked_is_processed_from_the_saved_copy(pipeline):
    site = FakeSite({"/housing": with_validators()})
    pipeline.run(site, [source("/housing")])
    pipeline.chunk_file().unlink()

    summary = pipeline.run(site, [source("/housing")], force=True)

    assert site.requests[-1].headers["if-none-match"] == ETAG
    assert summary.unchanged == [SITE + "/housing"]  # the page didn't change; its chunks are back
    assert json.loads(pipeline.chunk_file().read_text())["chunks"]


@pytest.mark.parametrize(
    ("meta", "due"),
    [
        (None, True),
        ({}, True),
        ({"checked_at": "not a time"}, True),
        ({"fetched_at": "2026-10-01T00:00:00+00:00"}, True),  # older metadata has no checked_at
        (
            {"checked_at": "2026-10-01T12:00:00+00:00", "fetched_at": "2026-09-01T00:00:00+00:00"},
            False,
        ),
    ],
)
def test_is_due(meta, due):
    now = datetime.fromisoformat("2026-10-02T00:00:00+00:00")
    assert is_due(meta, "medium", INTERVALS, now) is due


def test_due_slack_is_shorter_than_every_configured_interval():
    intervals = load_check_intervals()
    assert intervals["fast"] == timedelta(0)
    assert all(DUE_SLACK < interval for interval in intervals.values() if interval)


def test_summary_reports_every_count():
    summary = Summary(
        unchanged=["a", "b"], changed=["c"], new=["d"], failed=["e: x"], not_due=["f"]
    )
    assert str(summary) == (
        "Checked 5: 2 unchanged, 1 changed, 1 new, 1 failed. Skipped 1 not due."
    )
