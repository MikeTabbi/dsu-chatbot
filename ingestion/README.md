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
