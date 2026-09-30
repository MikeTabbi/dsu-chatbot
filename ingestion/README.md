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

- **Campus buildings:** no page lists campus buildings or their locations (e.g. "Where's the MLK
  Building?").
- **Changing majors:** no clear page explains how to switch majors.

## Crawling notes

desu.edu's robots.txt sets `Crawl-delay: 10`; the crawler should honor it.
