"""Service-to-service authentication for the /internal routes.

Those routes are how the orchestrator drives an analysis: they register it,
trigger collection, compute, and complete it. They write, they cost money to
call, and none of them belongs in a browser.
"""

import hmac

from fastapi import Header, HTTPException

from app.core.config import settings


def require_internal_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Refuse unless the caller carries the shared secret.

    With no secret configured every call is refused, rather than every call
    being allowed. The two are indistinguishable from outside a running
    deployment, and only one of them is safe to get wrong.

    Compared with hmac.compare_digest so a wrong token takes the same time to
    reject whatever its prefix, which is the cheap habit rather than a real
    threat model for a secret that travels inside the cluster.
    """
    if not settings.internal_api_token:
        raise HTTPException(
            status_code=503,
            detail="INTERNAL_API_TOKEN is not configured, so internal routes are closed.",
        )
    if x_internal_token is None or not hmac.compare_digest(x_internal_token, settings.internal_api_token):
        raise HTTPException(status_code=401, detail="Invalid or missing X-Internal-Token.")
