"""
Azure AD SSO authentication endpoints.

GET  /api/auth/config  — Returns SSO configuration for the frontend.
POST /api/auth/verify  — Validates an Azure AD ID token (JWT) server-side.

Environment variables required to enable SSO:
    AZURE_AD_CLIENT_ID   — App (client) ID from the Azure AD SPA app registration
    AZURE_AD_TENANT_ID   — Azure AD tenant ID (default: DCRI Duke tenant)

When AZURE_AD_CLIENT_ID is not set, /api/auth/config returns {"sso_enabled": false}
and the frontend shows the "SSO requires setup" placeholder. All other functionality
continues to work — SSO is purely opt-in identity attribution, not an auth gate.

Token validation notes:
    - Uses PyJWT with RS256 algorithm
    - JWKS fetched from Microsoft's well-known endpoint and cached in-memory for 1 hour
    - Validates: signature, iss, aud, and exp claims
    - On failure raises HTTP 401 — never leaks details about the failure reason to the
      caller (only logs server-side)
"""

import os
import time
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# JWKS cache — simple in-memory dict, refreshed every hour
# ---------------------------------------------------------------------------

_jwks_cache: dict = {}          # {"keys": [...], "cached_at": float}
_JWKS_TTL_SECONDS = 3600        # 1 hour


def _get_jwks(tenant_id: str) -> list[dict]:
    """
    Fetch and cache Microsoft's public key set for the given tenant.

    The keys are used to verify RS256 JWT signatures on ID tokens issued by
    Azure AD.  We cache them for 1 hour to avoid hammering the JWKS endpoint
    on every request, but still pick up key rotations within a reasonable time.

    Returns a list of JWK dicts as returned by Microsoft's discovery endpoint.
    """
    import requests as _requests

    now = time.time()
    cached = _jwks_cache.get(tenant_id)
    if cached and (now - cached["cached_at"]) < _JWKS_TTL_SECONDS:
        return cached["keys"]

    jwks_url = (
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    )
    try:
        resp = _requests.get(jwks_url, timeout=10)
        resp.raise_for_status()
    except Exception as exc:
        logger.error("Failed to fetch JWKS from %s: %s", jwks_url, exc)
        raise HTTPException(
            status_code=503,
            detail="Unable to reach Microsoft identity service to verify token.",
        )

    keys = resp.json().get("keys", [])
    _jwks_cache[tenant_id] = {"keys": keys, "cached_at": now}
    return keys


def _verify_id_token(id_token: str, client_id: str, tenant_id: str) -> dict:
    """
    Validate an Azure AD ID token and return its decoded claims.

    Raises HTTPException(401) if the token is invalid, expired, or fails
    any claim check.  Deliberately returns a generic 401 to the caller
    without leaking specific failure details (details are logged server-side).

    Checks performed:
        1. Decode RS256 signature against Microsoft's published JWKS
        2. iss == https://login.microsoftonline.com/{tenant_id}/v2.0
        3. aud == client_id
        4. exp > now  (PyJWT checks this automatically when verify_exp=True)
    """
    try:
        import jwt
        from jwt import PyJWKClient
    except ImportError:
        logger.error(
            "PyJWT is not installed. Add 'PyJWT>=2.8.0' to requirements-bot.txt "
            "and run 'pip install -r requirements-bot.txt'."
        )
        raise HTTPException(
            status_code=503,
            detail="Server configuration error: JWT library not available.",
        )

    jwks_uri = (
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    )
    expected_issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"

    try:
        jwks_client = PyJWKClient(jwks_uri, cache_keys=True, lifespan=_JWKS_TTL_SECONDS)
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)

        claims = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=expected_issuer,
            options={"verify_exp": True},
        )
        return claims

    except jwt.ExpiredSignatureError:
        logger.warning("Rejected Azure AD token: token has expired.")
        raise HTTPException(status_code=401, detail="Token has expired.")

    except jwt.InvalidAudienceError:
        logger.warning("Rejected Azure AD token: audience claim mismatch.")
        raise HTTPException(status_code=401, detail="Invalid token.")

    except jwt.InvalidIssuerError:
        logger.warning("Rejected Azure AD token: issuer claim mismatch.")
        raise HTTPException(status_code=401, detail="Invalid token.")

    except jwt.PyJWTError:
        logger.warning("Rejected Azure AD token: JWT validation failed.")
        raise HTTPException(status_code=401, detail="Invalid token.")

    except Exception:
        logger.error("Unexpected error during Azure AD token verification.")
        raise HTTPException(status_code=401, detail="Invalid token.")


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AuthConfigResponse(BaseModel):
    sso_enabled: bool
    client_id: str = ""
    tenant_id: str = ""


class VerifyTokenRequest(BaseModel):
    id_token: str


class VerifyTokenResponse(BaseModel):
    valid: bool
    email: str = ""
    name: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/config", response_model=AuthConfigResponse)
async def get_auth_config() -> AuthConfigResponse:
    """
    Return SSO configuration for the web frontend.

    The frontend calls this on page load to decide whether to show the
    MSAL sign-in button or the "SSO requires setup" placeholder.

    When AZURE_AD_CLIENT_ID is not set in the environment, SSO is disabled
    and the app continues to function using the server service account for
    all Jira operations — SSO is purely additive identity attribution.
    """
    client_id = os.environ.get("AZURE_AD_CLIENT_ID", "").strip()
    # Fall back to the known DCRI Duke tenant if not explicitly overridden
    tenant_id = os.environ.get(
        "AZURE_AD_TENANT_ID", "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
    ).strip()

    if not client_id or client_id == "CHANGE_ME":
        return AuthConfigResponse(sso_enabled=False)

    return AuthConfigResponse(
        sso_enabled=True,
        client_id=client_id,
        tenant_id=tenant_id,
    )


@router.post("/verify", response_model=VerifyTokenResponse)
async def verify_token(req: VerifyTokenRequest) -> VerifyTokenResponse:
    """
    Validate an Azure AD ID token received from MSAL.js in the browser.

    The frontend obtains an ID token via MSAL loginPopup/loginRedirect and
    POSTs it here for server-side validation before trusting it.  This
    prevents a client-side-only trust model where a forged token could be
    accepted.

    On success, returns the user's email and display name extracted from
    the verified token claims.  These are used solely for attribution in
    Jira ticket comments — not for access control.

    Raises HTTP 401 if the token is missing, malformed, expired, or fails
    signature verification.
    """
    if not req.id_token or not req.id_token.strip():
        raise HTTPException(status_code=401, detail="No token provided.")

    client_id = os.environ.get("AZURE_AD_CLIENT_ID", "").strip()
    tenant_id = os.environ.get(
        "AZURE_AD_TENANT_ID", "cb72c54e-4a31-4d9e-b14a-1ea36dfac94c"
    ).strip()

    if not client_id or client_id == "CHANGE_ME":
        raise HTTPException(
            status_code=503,
            detail="SSO is not configured on this server.",
        )

    claims = _verify_id_token(
        id_token=req.id_token,
        client_id=client_id,
        tenant_id=tenant_id,
    )

    # Extract user identity from standard OIDC claims
    email = claims.get("preferred_username") or claims.get("email") or claims.get("upn", "")
    name  = claims.get("name") or claims.get("given_name", "")

    return VerifyTokenResponse(valid=True, email=email, name=name)
