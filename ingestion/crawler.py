"""Politely fetch the pages in the source registry and save the raw responses to disk."""

import argparse
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ingestion.sources import ALLOWED_DOMAIN, Source, load_sources

USER_AGENT = "DSU-Chatbot-Crawler/0.1 (+https://github.com/MikeTabbi/dsu-chatbot)"
DEFAULT_OUTPUT_DIR = Path("data/raw")
DEFAULT_DELAY = 10.0  # seconds; robots.txt Crawl-delay wins if it is longer
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
MAX_REDIRECTS = 5
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    fetched_at: str
    topic: str
    content_type: str | None = None
    encoding: str | None = None
    redirects: list[str] = field(default_factory=list)
    html_file: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Crawler:
    """Fetches URLs one at a time, honoring robots.txt and a per-host delay between requests."""

    def __init__(
        self,
        output_dir: Path | str = DEFAULT_OUTPUT_DIR,
        *,
        delay: float = DEFAULT_DELAY,
        retries: int = DEFAULT_RETRIES,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.output_dir = Path(output_dir)
        self.delay = delay
        self.retries = retries
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=DEFAULT_TIMEOUT
        )
        self._sleep = sleep
        self._clock = clock
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}

    def crawl(self, sources: Iterable[Source]) -> list[FetchResult]:
        """Fetch and save every source. Failures are logged and recorded, never raised."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for source in sources:
            result = self.fetch(source)
            if result.ok:
                log.info("saved %s -> %s", source.url, result.html_file)
            else:
                log.warning("skipped %s: %s", source.url, result.error)
            results.append(result)
        self._write_manifest(results)
        return results

    def fetch(self, source: Source) -> FetchResult:
        result = FetchResult(
            url=source.url, final_url=source.url, status=None, fetched_at="", topic=source.topic
        )
        url = source.url
        for _ in range(MAX_REDIRECTS + 1):
            if error := self._check_allowed(url):
                result.error = error
                return result

            response, error = self._get(url)
            result.fetched_at = _now()
            if response is None:
                result.error = error
                return result
            result.status = response.status_code

            if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                url = urljoin(url, response.headers["location"])
                result.redirects.append(url)
                result.final_url = url
                log.info("redirect %s -> %s", response.url, url)
                continue
            if response.status_code != 200:
                result.error = f"HTTP {response.status_code}"
                return result

            result.content_type = response.headers.get("content-type")
            result.encoding = response.encoding
            self._save(result, response.content)
            return result

        result.error = f"more than {MAX_REDIRECTS} redirects"
        return result

    def _check_allowed(self, url: str) -> str | None:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)
        ):
            return f"off-domain url: {url}"
        if not self._robots_for(url).can_fetch(USER_AGENT, url):
            return f"disallowed by robots.txt: {url}"
        return None

    def _robots_for(self, url: str) -> RobotFileParser:
        origin = _origin(url)
        if origin in self._robots:
            return self._robots[origin]

        robots = RobotFileParser(origin + "/robots.txt")
        response, error = self._get(origin + "/robots.txt", follow_redirects=True)
        if response is None or response.status_code >= 500:
            # Can't tell what the site allows, so fetch nothing from it this run.
            log.warning("robots.txt unavailable for %s (%s); skipping host", origin, error)
            robots.disallow_all = True
        elif response.status_code in (401, 403):
            robots.disallow_all = True
        elif response.status_code >= 400:
            robots.allow_all = True
        else:
            robots.parse(response.text.splitlines())
        self._robots[origin] = robots
        return robots

    def _get(
        self, url: str, follow_redirects: bool = False
    ) -> tuple[httpx.Response | None, str | None]:
        """GET a URL, retrying timeouts, network errors, and 5xx responses.

        Page redirects are not followed here so fetch() can check each hop against robots.txt.
        """
        error = None
        for attempt in range(self.retries + 1):
            self._wait_turn(url)
            try:
                response = self.client.get(url, follow_redirects=follow_redirects)
            except httpx.TimeoutException as e:
                error = f"timeout: {e!r}"
            except httpx.HTTPError as e:
                error = f"request failed: {e!r}"
            else:
                if response.status_code < 500 or attempt == self.retries:
                    return response, None
                error = f"HTTP {response.status_code}"
            log.info("attempt %d/%d for %s failed: %s", attempt + 1, self.retries + 1, url, error)
        return None, error

    def _wait_turn(self, url: str) -> None:
        host = urlparse(url).netloc
        last = self._last_request.get(host)
        if last is not None:
            wait = last + self._delay_for(url) - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _delay_for(self, url: str) -> float:
        robots = self._robots.get(_origin(url))
        crawl_delay = robots.crawl_delay(USER_AGENT) if robots else None
        return max(self.delay, float(crawl_delay or 0))

    def _save(self, result: FetchResult, body: bytes) -> None:
        """Write the raw body to <stem>.html and its metadata to <stem>.json."""
        stem = _file_stem(result.url)
        result.html_file = f"{stem}.html"
        (self.output_dir / result.html_file).write_bytes(body)
        meta = asdict(result)
        del meta["error"]
        (self.output_dir / f"{stem}.json").write_text(json.dumps(meta, indent=2) + "\n")

    def _write_manifest(self, results: list[FetchResult]) -> None:
        manifest = {
            "user_agent": USER_AGENT,
            "finished_at": _now(),
            "pages": [asdict(r) for r in results],
        }
        (self.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def _file_stem(url: str) -> str:
    """Readable, collision-safe file name for a URL, e.g. 'student-life_housing-dining-1a2b3c4d'."""
    parsed = urlparse(url)
    slug = re.sub(r"[^a-z0-9]+", "_", parsed.path.lower()).strip("_") or "index"
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{slug[:100]}-{digest}"


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")
    parser.add_argument("--limit", type=int, help="only crawl the first N sources")
    parser.add_argument("--topic", help="only crawl sources with this topic")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, help="minimum seconds between requests"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    sources = [s for s in load_sources() if args.topic in (None, s.topic)][: args.limit]
    results = Crawler(args.out, delay=args.delay).crawl(sources)
    saved = sum(r.ok for r in results)
    print(f"Saved {saved}/{len(results)} pages to {args.out}")


if __name__ == "__main__":
    main()
