from enterprise_research.domain.source import (
    Source,
    SourceType,
    TrustLevel,
)


def test_source_defaults():

    source = Source(
        url="https://example.com/report",
        title="Market Report",
    )

    assert source.source_type == (
        SourceType.WEB_PAGE
    )

    assert source.trust_level == (
        TrustLevel.MEDIUM
    )


def test_source_metadata():

    source = Source(
        url="https://example.com/report",
        title="Annual Report",
        publisher="Example Corp",
        trust_level=TrustLevel.HIGH,
        content_hash="abc123",
    )


    assert source.publisher == (
        "Example Corp"
    )

    assert source.content_hash == (
        "abc123"
    )

    assert source.trust_level == (
        TrustLevel.HIGH
    )
