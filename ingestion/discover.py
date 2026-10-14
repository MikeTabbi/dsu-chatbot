"""Suggest new pages for sources.yaml from the desu.edu sitemaps. Never edits sources.yaml.

For each host in discovery.yaml, reads /sitemap.xml and the HTML /sitemap page (after checking
the host's robots.txt), filters the URLs with the include and exclude rules, then fetches each
remaining page once, at the host's crawl delay, to read its canonical URL and title. Pages that
share a canonical URL, or that are already in sources.yaml, are dropped.

Writes, for review:
- candidates.csv: one row per suggested page. Mark the ones to add with `yes` in the approve
  column, then run `python -m ingestion.approve`.
- pdfs.csv: PDF links seen in the sitemaps and on the fetched pages. They are not fetched.
"""

import argparse
import csv
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import yaml
from bs4 import BeautifulSoup

from ingestion import extract
from ingestion.crawler import DEFAULT_DELAY, Crawler
from ingestion.sources import ALLOWED_DOMAIN, CHANGE_FREQUENCIES, load_sources
from ingestion.sources import DEFAULT_PATH as DEFAULT_SOURCES_PATH

DEFAULT_RULES_PATH = Path(__file__).with_name("discovery.yaml")
DEFAULT_OUTPUT_DIR = Path("data/discovery")
CANDIDATE_COLUMNS = (
    "approve",
    "url",
    "topic",
    "change_frequency",
    "title",
    "notes",
    "word_count",
    "found_in",
    "duplicate_urls",
)
TITLE_SUFFIX = re.compile(r"\s*\|\s*(Delaware State University|DSU)\s*$", re.IGNORECASE)

log = logging.getLogger(__name__)


class RulesError(ValueError):
    """The discovery rules file is missing, malformed, or has an invalid rule."""


@dataclass(frozen=True)
class IncludeRule:
    topic: str
    change_frequency: str
    patterns: tuple[re.Pattern, ...]


@dataclass(frozen=True)
class Rules:
    hosts: tuple[str, ...]
    exclude: tuple[tuple[str, re.Pattern], ...]  # (pattern as written, compiled)
    include: tuple[IncludeRule, ...]

    def classify(self, url: str) -> tuple[IncludeRule | None, str]:
        """The include rule for url, or None and why it was dropped."""
        key = url_key(url)
        for written, pattern in self.exclude:
            if pattern.fullmatch(key):
                return None, f"excluded by {written}"
        for rule in self.include:
            if any(p.fullmatch(key) for p in rule.patterns):
                return rule, ""
        return None, "no include rule"


def load_rules(path: Path | str = DEFAULT_RULES_PATH) -> Rules:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text())
    except OSError as e:
        raise RulesError(f"cannot read {path}: {e}") from e
    except yaml.YAMLError as e:
        raise RulesError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise RulesError(f"{path} must be a mapping with hosts, exclude, and include")

    hosts = data.get("hosts")
    if not isinstance(hosts, list) or not hosts or not all(_on_domain(h) for h in hosts):
        raise RulesError(f"{path}: hosts must be a list of {ALLOWED_DOMAIN} host names")

    exclude = data.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(p, str) and p for p in exclude):
        raise RulesError(f"{path}: exclude must be a list of patterns")

    include = data.get("include")
    if not isinstance(include, list) or not include:
        raise RulesError(f"{path}: include must be a non-empty list of rules")
    rules = []
    for i, rule in enumerate(include):
        where = f"{path}: include[{i}]"
        if not isinstance(rule, dict) or set(rule) != {"topic", "change_frequency", "paths"}:
            raise RulesError(f"{where} must have exactly topic, change_frequency, and paths")
        if not isinstance(rule["topic"], str) or not re.fullmatch(r"[a-z][a-z-]*", rule["topic"]):
            raise RulesError(f"{where} topic must be a short lowercase label: {rule['topic']!r}")
        if rule["change_frequency"] not in CHANGE_FREQUENCIES:
            raise RulesError(
                f"{where} change_frequency must be one of {', '.join(CHANGE_FREQUENCIES)}"
            )
        paths = rule["paths"]
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            raise RulesError(f"{where} paths must be a non-empty list of patterns")
        rules.append(
            IncludeRule(rule["topic"], rule["change_frequency"], tuple(map(compile_pattern, paths)))
        )

    return Rules(
        hosts=tuple(h.lower() for h in hosts),
        exclude=tuple((p, compile_pattern(p)) for p in exclude),
        include=tuple(rules),
    )


def compile_pattern(pattern: str) -> re.Pattern:
    """A rules pattern as a regex: * within a segment, ** across segments, $ for this page only.
    Without $, the pattern also matches every page under it."""
    exact = pattern.endswith("$")
    body = pattern.removesuffix("$").strip("/")
    regex = ""
    for part in re.split(r"(\*\*|\*)", body):
        regex += {"**": ".*", "*": "[^/]*"}.get(part, re.escape(part))
    return re.compile(regex if exact else regex + "(/.*)?")


def normalize_url(url: str) -> str | None:
    """One spelling per desu.edu page: https, lowercase host, bare desu.edu as www, no query,
    fragment, repeated slashes, or trailing slash. None for anything off desu.edu."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not _on_domain(host):
        return None
    if host == ALLOWED_DOMAIN:
        host = "www." + ALLOWED_DOMAIN
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    return f"https://{host}{path}"


def url_key(url: str) -> str:
    """What rule patterns match against: the normalized URL without its scheme."""
    return (normalize_url(url) or url).removeprefix("https://")


def is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def parse_sitemap_xml(text: str | bytes) -> tuple[list[str], list[str]]:
    """The page URLs of a sitemap, and the child sitemaps of a sitemap index. A document that
    isn't valid XML has neither."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as e:
        log.warning("sitemap is not valid XML: %s", e)
        return [], []
    pages, children = [], []
    for entry in root:
        kind = entry.tag.rsplit("}", 1)[-1]  # with or without the sitemaps.org namespace
        loc = next((e for e in entry if e.tag.rsplit("}", 1)[-1] == "loc"), None)
        url = (loc.text or "").strip() if loc is not None else ""
        if url and kind == "url":
            pages.append(url)
        elif url and kind == "sitemap":
            children.append(url)
    return pages, children


def parse_sitemap_html(html: str | bytes, base_url: str) -> list[tuple[str, str]]:
    """(url, link text) for every link in a Drupal site map page's menus.

    Links inside hidden (.element-invisible) mega-menu panels are skipped; they repeat menu
    entries and add off-site links. Falls back to the content region if there are no menus.
    """
    soup = BeautifulSoup(html, "html.parser")
    region = soup.select_one(".site-map-menus") or soup.select_one("main") or soup
    links = []
    for a in region.find_all("a", href=True):
        if a.find_parent(class_="element-invisible"):
            continue
        href = a["href"].strip()
        if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
            links.append((urljoin(base_url, href), a.get_text(" ", strip=True)))
    return links


def page_links(html: str | bytes, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    return [urljoin(base_url, a["href"].strip()) for a in soup.find_all("a", href=True)]


@dataclass
class Candidate:
    url: str
    topic: str
    change_frequency: str
    title: str = ""
    word_count: int | None = None
    found_in: set[str] = field(default_factory=set)  # sitemap.xml, sitemap
    duplicate_urls: list[str] = field(default_factory=list)

    def row(self) -> dict[str, str]:
        return {
            "approve": "",
            "url": self.url,
            "topic": self.topic,
            "change_frequency": self.change_frequency,
            "title": self.title,
            "notes": "",
            "word_count": "" if self.word_count is None else str(self.word_count),
            "found_in": " ".join(sorted(self.found_in)),
            "duplicate_urls": " ".join(self.duplicate_urls),
        }


@dataclass
class Report:
    """What discovery found, for the printed summary."""

    hosts: dict[str, list[str]] = field(default_factory=dict)  # host -> status lines
    found: int = 0
    dropped: Counter = field(default_factory=Counter)  # reason -> count
    registered: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)  # "url -> canonical"
    failed: list[str] = field(default_factory=list)  # "url: error"
    candidates: list[Candidate] = field(default_factory=list)
    pdfs: dict[str, set[str]] = field(default_factory=dict)  # pdf url -> where it was linked

    def __str__(self) -> str:
        lines = []
        for host, status in self.hosts.items():
            lines.append(f"{host}:")
            lines += [f"  {s}" for s in status]
        lines.append(
            f"Found {self.found} URLs. Already in sources.yaml: {len(self.registered)}. "
            f"Duplicates of another URL: {len(self.duplicates)}. Failed to fetch: "
            f"{len(self.failed)}. PDF links: {len(self.pdfs)}."
        )
        lines += [f"  dropped {n}: {reason}" for reason, n in self.dropped.most_common()]
        lines += [f"  duplicate: {d}" for d in self.duplicates]
        lines += [f"  failed: {f}" for f in self.failed]
        topics = Counter(c.topic for c in self.candidates)
        lines.append(f"Candidates: {len(self.candidates)}")
        lines += [f"  {topic}: {n}" for topic, n in sorted(topics.items())]
        return "\n".join(lines)


def discover(
    crawler: Crawler,
    rules: Rules,
    registered: set[str],
    *,
    fetch_pages: bool = True,
) -> Report:
    """Read every host's sitemaps, apply the rules, and (with fetch_pages) fetch each remaining
    page once to dedupe by canonical URL. registered: URLs already in sources.yaml."""
    report = Report()
    registered_keys = {normalize_url(u) for u in registered}
    found: dict[str, Candidate] = {}  # normalized URL -> candidate, before filtering
    titles: dict[str, str] = {}

    for host in rules.hosts:
        status = report.hosts.setdefault(host, [])
        origin = f"https://{host}"
        status.append(crawler.robots_report(origin + "/"))
        for url in _read_xml_sitemap(crawler, origin, status):
            _add_found(found, url, "sitemap.xml", report)
        response, final_url, error = crawler.get(origin + "/sitemap")
        status.append(f"/sitemap: {'found' if response is not None and not error else error}")
        if response is not None and not error:
            for url, text in parse_sitemap_html(response.content, final_url):
                key = _add_found(found, url, "sitemap", report)
                if key and text:
                    titles.setdefault(key, text)

    report.found = len(found)
    allowed_hosts = set(rules.hosts)
    queue: dict[str, list[Candidate]] = {}
    for key, candidate in found.items():
        if urlparse(key).hostname not in allowed_hosts:
            report.dropped["host not in discovery.yaml"] += 1
            continue
        if key in registered_keys:
            report.registered.append(key)
            continue
        rule, reason = rules.classify(key)
        if rule is None:
            report.dropped[reason] += 1
            continue
        candidate.topic, candidate.change_frequency = rule.topic, rule.change_frequency
        candidate.title = titles.get(key, "")
        queue.setdefault(urlparse(key).hostname, []).append(candidate)

    # Round-robin across hosts, so one host's crawl delay doesn't hold up the others.
    ordered = [c for batch in zip_longest(*queue.values()) for c in batch if c is not None]
    if not fetch_pages:
        report.candidates = ordered
        return report

    by_canonical: dict[str, Candidate] = {}
    for candidate in ordered:
        _fetch_candidate(crawler, rules, candidate, registered_keys, by_canonical, report)
    report.candidates = list(by_canonical.values())
    return report


def _read_xml_sitemap(crawler: Crawler, origin: str, status: list[str]) -> list[str]:
    """Page URLs from origin/sitemap.xml, following one level of sitemap index."""
    response, _, error = crawler.get(origin + "/sitemap.xml")
    if response is None or error:
        status.append(f"/sitemap.xml: {error}")
        return []
    pages, children = parse_sitemap_xml(response.content)
    for child in children:
        if urlparse(child).hostname != urlparse(origin).hostname:
            continue
        child_response, _, child_error = crawler.get(child)
        if child_response is None or child_error:
            status.append(f"{child}: {child_error}")
            continue
        pages += parse_sitemap_xml(child_response.content)[0]
    note = f" (index of {len(children)} sitemaps)" if children else ""
    status.append(f"/sitemap.xml: found, {len(pages)} URLs{note}")
    return pages


def _add_found(found: dict[str, Candidate], url: str, where: str, report: Report) -> str | None:
    if is_pdf(url):
        report.pdfs.setdefault(url, set()).add(where)
        return None
    key = normalize_url(url)
    if key is None:
        return None
    found.setdefault(key, Candidate(url=key, topic="", change_frequency="")).found_in.add(where)
    return key


def _fetch_candidate(
    crawler: Crawler,
    rules: Rules,
    candidate: Candidate,
    registered_keys: set[str | None],
    by_canonical: dict[str, Candidate],
    report: Report,
) -> None:
    response, final_url, error = crawler.get(candidate.url)
    if response is None or error:
        report.failed.append(f"{candidate.url}: {error}")
        return
    page = extract.extract(response.content, final_url, response.encoding)
    for link in page_links(response.content, final_url):
        if is_pdf(link) and normalize_url(link):
            report.pdfs.setdefault(link, set()).add(candidate.url)

    canonical = normalize_url(page.canonical_url or "") or normalize_url(final_url)
    if canonical is None or urlparse(canonical).hostname not in rules.hosts:
        canonical = candidate.url
    if canonical != candidate.url:
        report.duplicates.append(f"{candidate.url} -> {canonical}")
    if canonical in registered_keys:
        report.registered.append(candidate.url)
        return
    if canonical in by_canonical:
        by_canonical[canonical].duplicate_urls.append(candidate.url)
        by_canonical[canonical].found_in |= candidate.found_in
        return
    if canonical != candidate.url:
        rule, reason = rules.classify(canonical)
        if rule is None:
            report.dropped[f"canonical URL {reason}"] += 1
            return
        candidate.topic, candidate.change_frequency = rule.topic, rule.change_frequency
        candidate.duplicate_urls.append(candidate.url)
        candidate.url = canonical
    candidate.title = TITLE_SUFFIX.sub("", page.title or "") or candidate.title
    candidate.word_count = page.word_count
    by_canonical[canonical] = candidate


def write_candidates(candidates: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CANDIDATE_COLUMNS)
        writer.writeheader()
        for c in sorted(candidates, key=lambda c: (c.topic, c.url)):
            writer.writerow(c.row())


def write_pdfs(pdfs: dict[str, set[str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["url", "linked_from"])
        for url in sorted(pdfs):
            writer.writerow([url, " ".join(sorted(pdfs[url]))])


def _on_domain(host: object) -> bool:
    return isinstance(host, str) and (host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_PATH)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")
    parser.add_argument(
        "--no-fetch",
        action="store_true",
        help="only read the sitemaps; don't fetch pages (no canonical dedupe or page titles)",
    )
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY, help="minimum seconds between requests"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rules = load_rules(args.rules)
    registered = {s.url for s in load_sources(args.sources)}
    crawler = Crawler(args.out, delay=args.delay)
    report = discover(crawler, rules, registered, fetch_pages=not args.no_fetch)
    write_candidates(report.candidates, args.out / "candidates.csv")
    write_pdfs(report.pdfs, args.out / "pdfs.csv")
    print(report)
    print(f"Wrote {args.out / 'candidates.csv'} and {args.out / 'pdfs.csv'}")


if __name__ == "__main__":
    main()
