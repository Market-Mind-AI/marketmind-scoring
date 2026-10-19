class MarketMindCoreError(Exception):
    pass


class ExternalSourceUnavailableError(MarketMindCoreError):
    """Raised when one of the 4 collection services' HTTP APIs is unreachable or answers with an error."""

    pass


class EntityNotFoundError(MarketMindCoreError):
    pass
