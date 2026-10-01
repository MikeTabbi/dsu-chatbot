# Ingestion

Keeps the search index in sync with DSU sources.

Planned steps:
1. **Fetch** pages from the source list (sitemap first).
2. **Skip unchanged** pages using last-modified dates or a content fingerprint.
3. **Clean and chunk** text, keeping the source URL and date on every chunk.
4. **Upsert** new/changed chunks into the index.
5. **Delete** chunks for pages that were removed or moved.

Refresh schedule by source type:
- Slow-changing (catalog, policies): weekly or nightly
- Frequently changing (news, deadlines): hourly, or on publish if the website can notify us
- Live data (events, dining hours): not indexed; fetched at question time instead

## Adding a source

Sources live in [sources.yaml](sources.yaml). To add one, append an entry:

```yaml
  - url: https://www.desu.edu/some/page      # https, on desu.edu
    topic: admissions                        # short lowercase label
    change_frequency: medium                 # slow | medium | fast
    notes: What questions this page answers.
```

All four fields are required. Then check it loads:

```bash
python -m ingestion.sources   # prints source counts per topic, or the first error
pytest ingestion/tests
```

The loader rejects missing or empty fields, unknown fields, non-https or off-domain URLs,
duplicate URLs, and `change_frequency` values other than slow, medium, or fast.

## Known content gaps

Questions we expect but have no good source page for yet:

- **Campus buildings:** no page lists campus buildings or gives directions to them. A building
  shows up only when a page mentions it in passing: the Residential Halls page gives the MLK
  Building's address as part of the Housing office's contact info, so "Where's the MLK Building?"
  is answerable, but most buildings aren't.
- **Changing majors:** no clear page explains how to switch majors.
- **Sending SAT/ACT scores:** no page explains how to send scores to DSU or lists DSU's school
  code. The existing SAT/ACT page only shows students how to download their own score report.

## Crawling

```bash
python -m ingestion.crawler              # every source; ~10s per page
python -m ingestion.crawler --limit 3    # first 3 sources only
python -m ingestion.crawler --topic housing --out data/raw-test
```

The crawler reads [sources.yaml](sources.yaml) and saves each fetched page to `data/raw/` (gitignored):

- `<name>.html`: the raw response body, byte for byte
- `<name>.json`: `url`, `final_url`, `status`, `fetched_at` (UTC), `content_type`, `encoding`,
  `redirects`, `topic`, `html_file`
- `manifest.json`: every URL from the run, including skipped ones and why they were skipped

`<name>` is the URL path plus a short hash, e.g. `student_life_housing_dining-1a2b3c4d`.

Politeness and failure handling:

- Identifies as `DSU-Chatbot-Crawler/0.1 (+https://github.com/MikeTabbi/dsu-chatbot)`.
- Checks robots.txt before every request, including redirect targets. desu.edu sets
  `Crawl-delay: 10`. The crawler waits for the longer of that and `--delay` (default 10s) between
  requests. If robots.txt can't be fetched, the host is skipped.
- Timeouts, network errors, and 5xx responses are retried twice, then skipped. 404s and other
  4xx responses are skipped. Redirects are followed (up to 5) on desu.edu when robots.txt allows
  it, and the final URL is recorded. Off-domain redirects are skipped. Skipped pages are logged
  and listed in the manifest, and the crawl continues.
- Sitemap discovery (<https://www.desu.edu/sitemap>) is not implemented yet. For now only
  registry URLs are crawled.

## Extracting page content

```bash
python -m ingestion.extract                                  # data/raw -> data/extracted
python -m ingestion.extract --raw data/raw-test --out data/extracted-test
```

Every desu.edu page carries about 120 KB of site chrome (mega-menu, mobile nav, header, footer)
around a few KB of content. The extractor keeps only the content region, `main [role=main]` in
the site's Drupal 7 theme, and removes the chrome that sits inside it: the breadcrumb, the
section menu sidebar, the "Start your journey here" box, and the landing-page quick-link bar. If
a page has no content region, it falls back to `<body>` with the header, footer, nav, and cookie
banner removed, and records a warning.

For each `<name>.json` the crawler wrote, it writes `data/extracted/<name>.json` with:

- `text`: the content as Markdown. Headings, paragraphs, lists, and tables are kept. Links become
  `[text](absolute url)` so answers can cite them. Embedded videos become an `[Embedded video](url)`
  link. Images are dropped.
- `title`, `canonical_url`, `modified_time` (`article:modified_time`, else `og:updated_time`),
  `breadcrumb` (list of `{title, url}`)
- `url`, `final_url`, `topic`, `fetched_at`, `raw_file`: carried over from the crawl
- `container`: the selector that matched the content region, or `fallback`
- `word_count`, `low_text`, `warnings`: pages under 50 words (e.g. the SAT/ACT page, which is
  mostly a video and images) get `low_text: true` and a warning, and are logged, so they can be
  reviewed instead of silently producing near-empty chunks.

When adding a page from a new part of the site, run the extractor and check its output for menu
text. New in-content chrome goes in `NOISE_SELECTORS` in [extract.py](extract.py).

## Chunking

```bash
python -m ingestion.chunk                                    # data/extracted -> data/chunks
python -m ingestion.chunk --extracted data/extracted-test --out data/chunks-test
```

The chunker reads the extractor's Markdown and splits it on headings first: every heading starts
a new section, and each chunk records its **heading path**, e.g.
`Housing & Dining > Freshman Residence Halls > Meta V. Jenkins Hall`. Heading lines are not
repeated in `text`, so retrieval (#12) indexes `heading_path` together with `text`. Headings
with no text of their own (e.g. `## Freshman Residence Halls`, followed straight away by the
first hall) don't produce a chunk, but still appear in their children's paths. Text before the
first heading uses the page title as its path.

A section longer than **300 words** is split into pieces of at most 300 words, with about **50
words of overlap** (whole sentences, list items, or paragraphs) at the start of each following
piece. Why these numbers:

- 300 words is roughly 400 tokens, which is enough for a full answer to a factual question
  (a hall description, a deadline with its conditions) and small enough that a retrieved chunk is
  mostly about one thing. Today's pages are 150 to 600 words, so nearly every section fits whole
  and the split only kicks in for long pages such as the course catalog.
- 50 words (about two sentences) of overlap keeps a sentence that depends on the one before it
  from losing its context at a cut. More overlap mostly stores duplicate text.
- Size is counted in words with the extractor's `word_count` rule (link URLs don't count), so no
  tokenizer is needed. Common embedding models accept thousands of tokens per input, so the extra
  tokens used by link URLs are not a problem.

Cuts only fall between whole blocks, then list items or lines, then sentences. A single sentence
over 300 words is cut into 300-word windows as a last resort. **Tables** are split only between
rows, and every piece repeats the header row and its `---` line, so each piece still says which
column is which hall. Table rows are not repeated as overlap. A single row longer than 300 words
is kept whole, even though that chunk goes over the limit.

For each `data/extracted/<name>.json`, it writes `data/chunks/<name>.json` with the page `url`
and a `chunks` list. A page with no text still gets a file with an empty list. Each chunk has:

- `chunk_id`: the first 16 hex digits of a SHA-256 over the source URL, heading path, and chunk
  text. The same page content always gives the same IDs. Editing one section changes only that
  section's IDs, so index sync (#13) can upsert new IDs and delete missing ones without
  re-embedding the rest of the page. `modified_time` is not part of the ID, so a re-publish with
  unchanged text keeps the same IDs. The rare identical chunk repeated under the same heading gets
  an occurrence number in the hash to stay unique.
- `chunk_index` (position within the page), `source_url` (canonical URL, else final URL),
  `title`, `heading_path`, `modified_time`, `topic`
- `low_text`: carried over from the page, so chunks from near-empty pages can be ranked down or
  reviewed
- `word_count`, `text` (Markdown)
