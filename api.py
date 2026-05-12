"""
backend/api.py
---------------
FastAPI application — the HTTP layer of the scraping platform.

Endpoints
---------
GET  /              → health check
POST /scrape        → run a scrape job, return JSON results
GET  /download/csv  → stream results as a downloadable CSV file

Run with:
    uvicorn backend.api:app --reload --port 8000

Or from the project root:
    python -m uvicorn backend.api:app --reload
"""

import sys
import os

# ── Make sure the project root is on sys.path so we can import our modules ───
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
import io

from scraper_service import scrape, build_csv_string


# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="WebScraper API",
    description="Professional web scraping platform powered by Selenium.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# Allow the frontend (any origin during dev; tighten in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ── Request / Response schemas ────────────────────────────────────────────────

class ScrapePayload(BaseModel):
    """Body of POST /scrape."""
    url: str = Field(..., examples=["https://example.com"],
                     description="Full URL of the website to scrape.")
    max_pages: int = Field(10, ge=1, le=50,
                           description="Maximum pages to crawl (1–50).")
    keyword: str | None = Field(None,
                                description="Optional keyword filter.")

    @field_validator("url")
    @classmethod
    def url_must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("url must not be blank")
        return v.strip()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/", tags=["health"])
def health_check():
    """Quick liveness probe."""
    return {"status": "ok", "service": "WebScraper API"}


@app.post("/scrape", tags=["scraping"])
def scrape_endpoint(payload: ScrapePayload):
    """
    Launch a scrape job and return all results in the response body.

    The call is synchronous — the HTTP response arrives only after the
    crawl is complete.  For long jobs the frontend polls a progress bar
    driven by the `pages_crawled` field in intermediate logs.
    """
    response = scrape(
        url=payload.url,
        max_pages=payload.max_pages,
        keyword=payload.keyword,
    )
    result = response.to_dict()

    if not response.success:
        # Return 422 with a structured error body — not a 500
        return JSONResponse(status_code=422, content=result)

    return result


@app.get("/download/csv", tags=["export"])
def download_csv(
    url: str = Query(..., description="The URL that was scraped."),
    max_pages: int = Query(10, ge=1, le=50),
    keyword: str | None = Query(None),
):
    """
    Re-run (or retrieve cached) scrape results and stream as a CSV download.

    In a production system you would cache the last job's results in Redis
    and serve from there.  Here we re-run to keep things dependency-free.
    """
    response = scrape(url=url, max_pages=max_pages, keyword=keyword)

    if not response.success:
        raise HTTPException(status_code=422, detail=response.error)

    csv_text = build_csv_string(response.data)

    return StreamingResponse(
        io.StringIO(csv_text),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="scrape_results.csv"'
        },
    )