"""POST /review (diff in, findings out) and GET /reviews/{id}."""

from __future__ import annotations

import asyncio
import os
import re
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.reviewer import Finding, Reviewer, ReviewerNotConfigured, ReviewFailed, build_client

router = APIRouter()

MAX_DIFF_CHARS = 200_000
MAX_STORED_REVIEWS = 500


PR_URL_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)/?(?:[?#].*)?$")


class ReviewRequest(BaseModel):
    diff: str = ""
    pr_url: str = ""


def _fetch_pr_diff(pr_url: str) -> str:
    """Download a public GitHub pull request's diff. Raises HTTPException with plain-English messages."""
    match = PR_URL_RE.match(pr_url.strip())
    if not match:
        raise HTTPException(
            status_code=400,
            detail="That does not look like a GitHub pull request link. It should look like https://github.com/owner/repo/pull/123.",
        )
    owner, repo, number = match.groups()
    headers = {"Accept": "application/vnd.github.diff", "User-Agent": "code-review-app"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https host
            return resp.read(MAX_DIFF_CHARS * 2).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            msg = "We could not open that pull request. Check the link, and note that private repositories are not supported."
        else:
            msg = "GitHub did not give us that pull request just now. Please try again or paste the diff instead."
        raise HTTPException(status_code=502, detail=msg) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail="We could not reach GitHub. Please try again or paste the diff instead.",
        ) from exc


class Review(BaseModel):
    id: str
    created_at: str
    findings: list[Finding]


class ReviewStore:
    """In-memory store, newest last, capped so it cannot grow without bound."""

    def __init__(self) -> None:
        self._items: OrderedDict[str, Review] = OrderedDict()

    def add(self, findings: list[Finding]) -> Review:
        review = Review(id=uuid.uuid4().hex[:12], created_at=datetime.now(UTC).isoformat(), findings=findings)
        self._items[review.id] = review
        while len(self._items) > MAX_STORED_REVIEWS:
            self._items.popitem(last=False)
        return review

    def get(self, review_id: str) -> Review | None:
        return self._items.get(review_id)


def _reviewer(request: Request) -> Reviewer:
    state = request.app.state
    if state.client is None:
        state.client = build_client()  # raises ReviewerNotConfigured when the key is missing
    return Reviewer(state.client)


@router.post("/review", response_model=Review)
async def create_review(body: ReviewRequest, request: Request) -> Review:
    diff = body.diff.strip()
    if not diff and body.pr_url.strip():
        diff = (await asyncio.to_thread(_fetch_pr_diff, body.pr_url)).strip()
    if not diff:
        raise HTTPException(status_code=400, detail="Paste a diff or a GitHub pull request link first, then ask for a review.")
    if len(diff) > MAX_DIFF_CHARS:
        raise HTTPException(
            status_code=413,
            detail="That diff is too large to review in one go. Try a smaller change or split it up.",
        )
    try:
        reviewer = _reviewer(request)
    except ReviewerNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    try:
        findings = await reviewer.review(diff)
    except ReviewFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return request.app.state.store.add(findings)


@router.get("/reviews/{review_id}", response_model=Review)
async def get_review(review_id: str, request: Request) -> Review:
    review = request.app.state.store.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="We could not find a review with that id.")
    return review
