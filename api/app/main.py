"""Backend API entry point."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.app.config import settings

app = FastAPI(title="DSU Chatbot API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.allowed_origins.split(",") if o.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Simple check that the server is up."""
    return {"status": "ok"}


# TODO: POST /chat
#   1. validate the question
#   2. retrieve relevant chunks from the search index
#   3. build the prompt from prompts/ + retrieved chunks
#   4. call Claude, return the answer with source links
#   5. log the exchange for feedback review
