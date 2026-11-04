"""Caller identity extracted from the bearer token.

AgentCore's inbound JWT authorizer has already validated the token's signature, issuer,
expiry and client_id before the request reaches the container, so here we only decode
the payload. Never use this module on requests that did not pass the authorizer.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass


class IdentityError(ValueError):
    """Raised when the caller identity cannot be determined."""


@dataclass(frozen=True)
class Caller:
    sub: str
    client_id: str | None
    username: str | None


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def caller_from_headers(headers: Mapping[str, str] | None) -> Caller:
    if not headers:
        raise IdentityError("missing Authorization header")
    auth = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
    if not auth:
        raise IdentityError("missing Authorization header")
    scheme, _, token = auth.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise IdentityError("Authorization header is not a bearer token")
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise IdentityError("bearer token is not a JWT")
    try:
        claims = json.loads(_b64url_decode(parts[1]))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        raise IdentityError("bearer token payload is not valid JSON") from e
    if not isinstance(claims, dict):
        raise IdentityError("bearer token payload is not an object")
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise IdentityError("bearer token has no sub claim")
    client_id = claims.get("client_id")
    username = claims.get("username")
    return Caller(
        sub=sub,
        client_id=client_id if isinstance(client_id, str) else None,
        username=username if isinstance(username, str) else None,
    )
