# Architecture

## Question path (every message)
1. **Chat widget** on dsu.edu sends the question to the backend.
2. **Backend API** (FastAPI on Azure App Service) validates it and applies rate limits.
3. **Retriever** searches the index for the most relevant DSU content.
4. **Claude** writes an answer using only that content, with source links.

## Data path (scheduled)
1. **DSU sources**: public website, catalog, office FAQs.
2. **Ingestion job**: crawls, skips unchanged pages, chunks, and removes deleted pages.
3. **Search index** (Azure AI Search): stores chunks with source URL and date.

## Feedback
Every answer is logged with thumbs up/down. Unanswered questions become a list of missing content.

## Handling content that changes
- Slow-changing content: scheduled crawl with change detection.
- Fast-changing content: more frequent crawl, or re-index on publish.
- Live data: fetched at question time via a tool, never indexed.

## Open questions
- Embeddings: is a provider besides Claude approved, or do we start with keyword search?
- Does DSU reach Claude directly through Anthropic or through Azure?
- What does dsu.edu run on, and can it notify us when pages are published?
