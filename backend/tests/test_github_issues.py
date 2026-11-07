from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.github_issues import _owner_and_repo, comment_on_issue, create_issue, issue_url

# --- _owner_and_repo -----------------------------------------------------------


def test_owner_and_repo_parses_a_dot_git_https_url():
    with patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"):
        assert _owner_and_repo() == ("hardmoneysniper", "is-my-train-screwed")


def test_owner_and_repo_parses_a_url_without_dot_git():
    with patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed"):
        assert _owner_and_repo() == ("hardmoneysniper", "is-my-train-screwed")


def test_owner_and_repo_handles_a_trailing_slash():
    with patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed/"):
        assert _owner_and_repo() == ("hardmoneysniper", "is-my-train-screwed")


def test_owner_and_repo_returns_none_for_empty_url():
    with patch("app.github_issues.settings.github_repo_url", ""):
        assert _owner_and_repo() is None


def test_owner_and_repo_returns_none_for_a_malformed_url():
    with patch("app.github_issues.settings.github_repo_url", "not-a-url"):
        assert _owner_and_repo() is None


# --- issue_url -------------------------------------------------------------------


def test_issue_url_builds_a_real_browsable_link():
    with patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"):
        assert issue_url(42) == "https://github.com/hardmoneysniper/is-my-train-screwed/issues/42"


def test_issue_url_returns_none_when_not_configured():
    with patch("app.github_issues.settings.github_repo_url", ""):
        assert issue_url(42) is None


def test_issue_url_does_not_require_a_token():
    # Unlike create_issue/comment_on_issue, a link to a public repo's
    # issue needs no auth -- issue_url must not gate on github_token.
    with (
        patch("app.github_issues.settings.github_token", ""),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
    ):
        assert issue_url(1) == "https://github.com/hardmoneysniper/is-my-train-screwed/issues/1"


# --- create_issue --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_returns_none_when_not_configured():
    with patch("app.github_issues.settings.github_token", ""):
        result = await create_issue("title", "body")
    assert result is None


@pytest.mark.asyncio
async def test_create_issue_returns_the_real_issue_number():
    mock_response = MagicMock(status_code=201)
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"number": 42})
    with (
        patch("app.github_issues.settings.github_token", "fake-token"),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
        patch("app.github_issues.httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post,
    ):
        result = await create_issue("A bug", "body text", labels=["bug"])

    assert result == 42
    call = mock_post.call_args
    assert call.args[0] == "https://api.github.com/repos/hardmoneysniper/is-my-train-screwed/issues"
    assert call.kwargs["json"] == {"title": "A bug", "body": "body text", "labels": ["bug"]}
    assert call.kwargs["headers"]["Authorization"] == "Bearer fake-token"


@pytest.mark.asyncio
async def test_create_issue_defaults_labels_to_an_empty_list():
    mock_response = MagicMock(status_code=201)
    mock_response.raise_for_status = MagicMock()
    mock_response.json = MagicMock(return_value={"number": 1})
    with (
        patch("app.github_issues.settings.github_token", "fake-token"),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
        patch("app.github_issues.httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post,
    ):
        await create_issue("title", "body")

    assert mock_post.call_args.kwargs["json"]["labels"] == []


@pytest.mark.asyncio
async def test_create_issue_returns_none_on_http_error():
    with (
        patch("app.github_issues.settings.github_token", "fake-token"),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
        patch("app.github_issues.httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.HTTPError("boom")),
    ):
        result = await create_issue("title", "body")

    assert result is None


@pytest.mark.asyncio
async def test_create_issue_returns_none_on_a_non_2xx_status():
    mock_response = MagicMock(status_code=422)
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError("422", request=MagicMock(), response=mock_response)
    )
    with (
        patch("app.github_issues.settings.github_token", "fake-token"),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
        patch("app.github_issues.httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response),
    ):
        result = await create_issue("title", "body")

    assert result is None


# --- comment_on_issue -----------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_on_issue_returns_false_when_not_configured():
    with patch("app.github_issues.settings.github_repo_url", ""):
        result = await comment_on_issue(1, "a comment")
    assert result is False


@pytest.mark.asyncio
async def test_comment_on_issue_returns_true_on_success():
    mock_response = MagicMock(status_code=201)
    mock_response.raise_for_status = MagicMock()
    with (
        patch("app.github_issues.settings.github_token", "fake-token"),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
        patch("app.github_issues.httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_response) as mock_post,
    ):
        result = await comment_on_issue(7, "me too")

    assert result is True
    call = mock_post.call_args
    assert call.args[0] == "https://api.github.com/repos/hardmoneysniper/is-my-train-screwed/issues/7/comments"
    assert call.kwargs["json"] == {"body": "me too"}


@pytest.mark.asyncio
async def test_comment_on_issue_returns_false_on_http_error():
    with (
        patch("app.github_issues.settings.github_token", "fake-token"),
        patch("app.github_issues.settings.github_repo_url", "https://github.com/hardmoneysniper/is-my-train-screwed.git"),
        patch("app.github_issues.httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=httpx.HTTPError("boom")),
    ):
        result = await comment_on_issue(7, "me too")

    assert result is False
