"""backend/app/github_issues.py

Minimal GitHub REST API client for the Feedback Triage Agent's issue
integration (2026-11-07) -- opens a real GitHub issue for each genuine,
non-duplicate piece of triaged feedback, and comments on the ORIGINAL
issue instead when a new item is flagged as a duplicate (see
app/agents/feedback_triage_agent.py). Uses the already-scoped, already-
rotated GITHUB_TOKEN (Issues read/write only, see CLAUDE.md's
credentials table) -- this was the token's whole reason for existing
since 2026-08-14, finally used.

Best-effort throughout, same discipline as app/push.py: a GitHub API
failure (rate limit, network error, revoked token, repo not configured)
must never break triage itself -- the feedback is still fully triaged
and stored locally regardless of whether the GitHub half of it succeeds.
Every function here returns a falsy result on failure rather than
raising.
"""
import logging

import httpx

from app.config import settings

GITHUB_API_BASE = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 15


def _owner_and_repo() -> tuple[str, str] | None:
    """Parses "owner/repo" out of a real git remote URL, e.g.
    "https://github.com/hardmoneysniper/is-my-train-screwed.git" ->
    ("hardmoneysniper", "is-my-train-screwed"). Returns None for an
    empty, malformed, or non-GitHub-shaped URL (fewer than two path
    segments) rather than raising -- callers treat that identically to
    "not configured"."""
    url = settings.github_repo_url.strip().rstrip("/")
    if not url:
        return None
    if url.endswith(".git"):
        url = url[: -len(".git")]
    parts = [p for p in url.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _is_configured() -> bool:
    return bool(settings.github_token) and _owner_and_repo() is not None


def issue_url(issue_number: int) -> str | None:
    """A browsable link to a real issue, or `None` if GitHub isn't
    configured (no repo URL) -- used by scripts/feedback_report.py to
    link out from the local review report. Doesn't require a token
    (unlike create_issue/comment_on_issue): a public repo's issues are
    viewable without auth, and this never calls the API at all."""
    owner_repo = _owner_and_repo()
    if owner_repo is None:
        return None
    owner, repo = owner_repo
    return f"https://github.com/{owner}/{repo}/issues/{issue_number}"


async def create_issue(title: str, body: str, labels: list[str] | None = None) -> int | None:
    """Opens a new GitHub issue, returning its real issue number. Returns
    `None` if not configured (no token/repo, e.g. local dev) or on any
    API-level failure -- a label name that doesn't already exist on the
    repo is auto-created by GitHub's own API, not an error condition
    here."""
    if not _is_configured():
        return None
    owner, repo = _owner_and_repo()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues",
                headers=_headers(),
                json={"title": title, "body": body, "labels": labels or []},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return response.json()["number"]
    except httpx.HTTPError:
        logging.exception("github_issues: failed to create issue")
        return None


async def comment_on_issue(issue_number: int, body: str) -> bool:
    """Posts a comment on an existing issue. Returns `True` on success,
    `False` on any failure (not configured, API error) -- never raises."""
    if not _is_configured():
        return False
    owner, repo = _owner_and_repo()
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{issue_number}/comments",
                headers=_headers(),
                json={"body": body},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            return True
    except httpx.HTTPError:
        logging.exception(f"github_issues: failed to comment on issue {issue_number}")
        return False
