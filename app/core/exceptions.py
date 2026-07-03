class MarketMindCoreError(Exception):
    pass


class ExternalSourceUnavailableError(MarketMindCoreError):
    """Raised when a read-only connection to one of the 4 collection services' databases fails."""

    pass


class EntityNotFoundError(MarketMindCoreError):
    pass


class JobNotFoundError(MarketMindCoreError):
    pass


class InsufficientDataError(MarketMindCoreError):
    """Raised when fewer than the minimum required indicators/countries are available to compute a MOS."""

    pass
