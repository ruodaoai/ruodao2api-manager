"""Shared error types."""


class UpstreamError(Exception):
    """An upstream call failed. `status` is what we should return to the client."""

    def __init__(self, message, status=502, code="", retryable=False, detail="",
                 account_id=""):
        super(UpstreamError, self).__init__(message)
        self.message = str(message or "upstream error")
        self.status = int(status or 502)
        self.code = str(code or "")
        self.retryable = bool(retryable)
        self.detail = str(detail or "")
        self.account_id = str(account_id or "")

    def as_openai(self):
        return {
            "error": {
                "message": self.message,
                "type": "upstream_error",
                "code": self.code or str(self.status),
            }
        }

    def __str__(self):
        return self.message


class AuthExpired(UpstreamError):
    def __init__(self, message="凭证已过期，请重新登录该账号", account_id=""):
        super(AuthExpired, self).__init__(message, status=401, code="auth_expired",
                                         retryable=False, account_id=account_id)


class RateLimited(UpstreamError):
    def __init__(self, message="上游限流", wait=60, account_id="",
                 code="rate_limited"):
        super(RateLimited, self).__init__(message, status=429, code=code,
                                          retryable=True, account_id=account_id)
        self.wait = max(1, int(wait or 60))
