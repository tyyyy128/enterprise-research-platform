from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from enterprise_research.domain import (
    RetrievalStatus,
    RetrievedDocument,
)


def test_success_factory_hashes_exact_content_and_counts_characters() -> None:
    content = "  # Title\n\nBody.  "

    document = RetrievedDocument.succeeded(
        source_id=uuid4(),
        requested_url=("https://example.com/a"),
        final_url=("https://example.com/final"),
        content=content,
        metadata={
            "provider": "fake",
        },
    )

    assert document.status == RetrievalStatus.SUCCEEDED

    assert document.content == content

    assert document.char_count == len(content)

    assert document.content_hash == sha256(content.encode("utf-8")).hexdigest()

    assert document.error is None


def test_failure_factory_preserves_failure_without_content() -> None:
    document = RetrievedDocument.failed(
        source_id=uuid4(),
        requested_url=("https://example.com/a"),
        final_url=("https://example.com/login"),
        error="  HTTP 403  ",
    )

    assert document.status == RetrievalStatus.FAILED

    assert str(document.final_url) == "https://example.com/login"

    assert document.content is None
    assert document.content_hash is None
    assert document.char_count == 0
    assert document.error == "HTTP 403"


def test_blank_success_content_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="non-blank content",
    ):
        RetrievedDocument.succeeded(
            source_id=uuid4(),
            requested_url=("https://example.com/a"),
            final_url=("https://example.com/a"),
            content="   \n",
        )


def test_blank_failure_error_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="non-blank error",
    ):
        RetrievedDocument.failed(
            source_id=uuid4(),
            requested_url=("https://example.com/a"),
            error="   ",
        )


def test_failed_state_cannot_smuggle_content() -> None:
    with pytest.raises(
        ValidationError,
        match="cannot contain content",
    ):
        RetrievedDocument(
            source_id=uuid4(),
            requested_url=("https://example.com/a"),
            status=RetrievalStatus.FAILED,
            content="unexpected",
            error="timeout",
        )


def test_success_state_rejects_mismatched_content_hash() -> None:
    with pytest.raises(
        ValidationError,
        match="does not match content",
    ):
        RetrievedDocument(
            source_id=uuid4(),
            requested_url=("https://example.com/a"),
            final_url=("https://example.com/a"),
            status=(RetrievalStatus.SUCCEEDED),
            content="body",
            content_type="text/markdown",
            content_hash="bad-hash",
        )
