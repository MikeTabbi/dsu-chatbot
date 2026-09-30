"""Extract each crawled page's main content and metadata from the raw HTML in data/raw/."""

import argparse
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag

DEFAULT_INPUT_DIR = Path("data/raw")
DEFAULT_OUTPUT_DIR = Path("data/extracted")
MIN_WORDS = 50  # pages with fewer words of content are flagged low_text

# The content region of the desu.edu theme (Drupal 7, dsu2016), most specific first.
CONTENT_SELECTORS = ("main [role=main]", "[role=main]", "main")
# Repeated site chrome that sits inside the content region.
NOISE_SELECTORS = (
    ".easy-breadcrumb",  # kept as metadata instead
    ".dsu-content--sidebar-1",  # section menu and quick links, repeated on every page in a section
    ".dsu-content--sub-footer",  # "Start your journey here" admissions box
    ".pane-bundle-landing-page-header-link-bar",  # Apply Now / Give / Title IX links
    "nav",
)
# Used only when no content region is found: strip the site-wide chrome from <body> instead.
CHROME_SELECTORS = (
    "header",
    "footer",
    "nav",
    ".element-invisible",
    ".alerts",
    ".dsu-mega-menu",
    "#sliding-popup",  # Drupal EU cookie compliance banner
    ".eu-cookie-compliance-banner",
)
DROP_TAGS = ("script", "style", "noscript", "template", "form", "svg", "button", "select")

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl", "dt",
    "figcaption", "figure", "footer", "header", "hr", "iframe", "li", "main", "ol", "p",
    "pre", "section", "summary", "table", "ul",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

log = logging.getLogger(__name__)


@dataclass
class Extraction:
    title: str | None
    canonical_url: str | None
    modified_time: str | None
    breadcrumb: list[dict[str, str | None]]
    container: str  # selector that matched the content region, or "fallback"
    text: str  # Markdown
    word_count: int
    low_text: bool


@dataclass
class ExtractedPage(Extraction):
    url: str = ""
    final_url: str = ""
    topic: str = ""
    fetched_at: str = ""
    raw_file: str = ""
    warnings: list[str] = field(default_factory=list)


def extract(html: str | bytes, url: str, encoding: str | None = None) -> Extraction:
    """Pull the main content (as Markdown) and page metadata out of one desu.edu page."""
    soup = BeautifulSoup(html, "html.parser", from_encoding=encoding)
    canonical = _canonical_url(soup, url)
    base = canonical or url
    breadcrumb = _breadcrumb(soup, base)

    for tag in soup.find_all(DROP_TAGS):
        tag.decompose()
    content, container = _content_region(soup)
    for selector in NOISE_SELECTORS:
        for tag in content.select(selector):
            tag.decompose()

    text = "\n\n".join(_Renderer(base).blocks(content))
    words = _word_count(text)
    return Extraction(
        title=_title(soup),
        canonical_url=canonical,
        modified_time=_meta(soup, "article:modified_time") or _meta(soup, "og:updated_time"),
        breadcrumb=breadcrumb,
        container=container,
        text=text,
        word_count=words,
        low_text=words < MIN_WORDS,
    )


def extract_page(meta_path: Path, output_dir: Path) -> ExtractedPage:
    """Extract the page a crawler .json file describes into output_dir, under the same name."""
    meta = json.loads(meta_path.read_text())
    raw_file = meta_path.with_name(meta["html_file"])
    url = meta.get("final_url") or meta["url"]
    result = extract(raw_file.read_bytes(), url, meta.get("encoding"))

    page = ExtractedPage(
        **asdict(result),
        url=meta["url"],
        final_url=url,
        topic=meta.get("topic", ""),
        fetched_at=meta.get("fetched_at", ""),
        raw_file=meta["html_file"],
    )
    if page.container == "fallback":
        page.warnings.append("content region not found; used <body> minus site chrome")
    if page.low_text:
        page.warnings.append(f"only {page.word_count} words of text (minimum {MIN_WORDS})")

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / meta_path.name).write_text(json.dumps(asdict(page), indent=2) + "\n")
    return page


def extract_all(
    input_dir: Path | str = DEFAULT_INPUT_DIR, output_dir: Path | str = DEFAULT_OUTPUT_DIR
) -> list[ExtractedPage]:
    """Extract every page the crawler saved in input_dir."""
    input_dir, output_dir = Path(input_dir), Path(output_dir)
    pages = []
    for meta_path in sorted(input_dir.glob("*.json")):
        if meta_path.name == "manifest.json":
            continue
        page = extract_page(meta_path, output_dir)
        for warning in page.warnings:
            log.warning("%s: %s", page.url, warning)
        pages.append(page)
    return pages


def _content_region(soup: BeautifulSoup) -> tuple[Tag, str]:
    for selector in CONTENT_SELECTORS:
        if (found := soup.select_one(selector)) is not None:
            return found, selector
    body = soup.body or soup
    for selector in CHROME_SELECTORS:
        for tag in body.select(selector):
            tag.decompose()
    return body, "fallback"


def _meta(soup: BeautifulSoup, name: str) -> str | None:
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    content = tag.get("content", "").strip() if tag else ""
    return content or None


def _title(soup: BeautifulSoup) -> str | None:
    if title := _meta(soup, "og:title"):
        return title
    for tag in (soup.title, soup.find("h1")):
        if tag and (text := tag.get_text(" ", strip=True)):
            return text
    return None


def _canonical_url(soup: BeautifulSoup, url: str) -> str | None:
    link = soup.find("link", rel="canonical")
    href = link.get("href", "").strip() if link else ""
    return urljoin(url, href) if href else None


def _breadcrumb(soup: BeautifulSoup, base: str) -> list[dict[str, str | None]]:
    trail = soup.select_one(".easy-breadcrumb")
    if trail is None:
        return []
    crumbs = []
    for segment in trail.find_all("span", recursive=False):
        if "easy-breadcrumb_segment-separator" in segment.get("class", []):
            continue
        link = segment if segment.get("href") else segment.find("a", href=True)
        crumbs.append(
            {
                "title": segment.get_text(" ", strip=True),
                "url": urljoin(base, link["href"]) if link else None,
            }
        )
    return crumbs


def _word_count(markdown: str) -> int:
    text = re.sub(r"\]\([^)]*\)", "]", markdown)  # link targets are not words
    return len(re.findall(r"\w+", text))


class _Renderer:
    """Turns an HTML subtree into Markdown blocks: headings, paragraphs, lists, and tables."""

    def __init__(self, base_url: str):
        self.base_url = base_url

    def blocks(self, element: Tag) -> list[str]:
        out: list[str] = []
        run: list[str] = []  # inline content waiting to become a paragraph

        def flush():
            if text := _tidy("".join(run)):
                out.append(text)
            run.clear()

        for child in element.children:
            if isinstance(child, Tag) and (child.name in BLOCK_TAGS or _has_blocks(child)):
                flush()
                out.extend(self.block(child))
            else:
                run.append(self.inline(child))
        flush()
        return out

    def block(self, tag: Tag) -> list[str]:
        if tag.name in HEADINGS:
            text = _tidy(self.inline(tag)).replace("\n", " ")
            return [f"{'#' * int(tag.name[1])} {text}"] if text else []
        if tag.name in ("ul", "ol"):
            return [lst] if (lst := self.list(tag)) else []
        if tag.name == "table":
            return [tbl] if (tbl := self.table(tag)) else []
        if tag.name == "iframe":
            src = tag.get("src", "").strip()
            if not src:
                return []
            kind = "video" if re.search(r"youtube|vimeo", src) else "content"
            return [f"[Embedded {kind}]({urljoin(self.base_url, src)})"]
        if tag.name == "blockquote":
            return ["\n".join("> " + line for line in b.splitlines()) for b in self.blocks(tag)]
        if tag.name == "hr":
            return []
        return self.blocks(tag)

    def list(self, tag: Tag) -> str:
        items = []
        for child in tag.find_all(True, recursive=False):
            blocks = self.blocks(child)
            if not blocks:
                continue
            marker = f"{len(items) + 1}. " if tag.name == "ol" else "- "
            body = "\n".join(blocks).replace("\n", "\n" + " " * len(marker))
            items.append(marker + body)
        return "\n".join(items)

    def table(self, tag: Tag) -> str:
        rows = []
        for tr in tag.find_all("tr"):
            cells = [
                _tidy(self.inline(cell)).replace("\n", " ").replace("|", "\\|")
                for cell in tr.find_all(["th", "td"], recursive=False)
            ]
            if any(cells):
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(r) + " |" for r in rows]
        lines.insert(1, "|" + " --- |" * width)
        return "\n".join(lines)

    def inline(self, node) -> str:
        if isinstance(node, NavigableString):
            # Comments, CDATA, doctypes, etc. are NavigableString subclasses; skip them.
            return re.sub(r"\s+", " ", str(node)) if type(node) is NavigableString else ""
        if node.name == "br":
            return "\n"
        if node.name == "img":
            return ""
        if "spamspan" in node.get("class", []):  # Drupal's obfuscated email: user [at] domain
            user, domain = node.select_one(".u"), node.select_one(".d")
            if user and domain:
                return f"{user.get_text(strip=True)}@{domain.get_text(strip=True)}"
        text = "".join(self.inline(child) for child in node.children)
        if node.name == "a":
            return self.link(node, text)
        if node.name in BLOCK_TAGS:  # a block nested in a table cell or list item line
            return f" {text} "
        return text

    def link(self, tag: Tag, text: str) -> str:
        href = tag.get("href", "").strip()
        label = _tidy(text).replace("\n", " ")
        if not label or not href or href.startswith(("#", "javascript:")):
            return text
        lead = " " if text[:1].isspace() else ""
        trail = " " if text[-1:].isspace() else ""
        return f"{lead}[{label}]({urljoin(self.base_url, href)}){trail}"


def _has_blocks(tag: Tag) -> bool:
    return tag.find(BLOCK_TAGS) is not None


def _tidy(text: str) -> str:
    """Collapse runs of spaces and trim each line, keeping line breaks from <br>."""
    lines = (re.sub(r" {2,}", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=DEFAULT_INPUT_DIR, help="crawler output")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_DIR, help="output directory")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    pages = extract_all(args.raw, args.out)
    flagged = sum(p.low_text for p in pages)
    print(f"Extracted {len(pages)} pages to {args.out} ({flagged} flagged low_text)")


if __name__ == "__main__":
    main()
