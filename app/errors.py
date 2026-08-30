class LedgerError(Exception):
    """A safe domain failure suitable for an RFC 9457 problem response."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        code: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code
