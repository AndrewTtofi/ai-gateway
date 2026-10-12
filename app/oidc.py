"""OIDC access tokens: verified against the identity provider's published keys (ADR 0029).

A token proves who is calling and nothing else. What they may do comes from the directory
and the policy, looked up on every request, so a token that is still valid stops working
the moment its user is deactivated or leaves a group.

Checks: signature (asymmetric algorithms only), issuer, audience, expiry. Keys come from
the provider's JWKS, cached by key id. An unknown key id triggers one refetch (the
provider rotated its keys), at most once a minute, so random key ids can't be used to
hammer the provider.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt

log = logging.getLogger(__name__)

# Asymmetric only. With a symmetric algorithm (HS256) the "public" key would be the
# signing key; PyJWT also refuses `none` when a list is given.
ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")
REFETCH_SECONDS = 60.0
LEEWAY_SECONDS = 30

Fetch = Callable[[str], Awaitable[dict[str, Any]]]


class TokenError(Exception):
    """The token is not acceptable. The message is safe to return to the caller."""


class KeysUnavailable(Exception):
    """The provider's keys can't be fetched and none are cached: nothing can be verified."""


async def _http_get_json(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
        resp = await client.get(url, headers={"accept": "application/json"})
        resp.raise_for_status()
        body = resp.json()
    if not isinstance(body, dict):
        raise ValueError("not a JSON object")
    return body


class Verifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        jwks_url: str = "",
        fetch: Fetch = _http_get_json,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer, self.audience = issuer, audience
        self._jwks_url = jwks_url  # empty: read from the provider's discovery document
        self._fetch, self._now = fetch, clock
        self._keys: dict[str, Any] = {}  # kid → public key
        self._fetched_at: float | None = None

    async def _load_keys(self) -> None:
        if not self._jwks_url:
            discovery = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
            self._jwks_url = str((await self._fetch(discovery))["jwks_uri"])
        jwks = jwt.PyJWKSet.from_dict(await self._fetch(self._jwks_url))
        self._keys = {k.key_id: k.key for k in jwks.keys if k.key_id}

    async def _key(self, kid: str) -> Any:
        stale = self._fetched_at is None or self._now() - self._fetched_at >= REFETCH_SECONDS
        if kid not in self._keys and stale:
            try:
                await self._load_keys()
            except Exception as exc:
                log.error("OIDC keys could not be fetched: %s", type(exc).__name__)
                if not self._keys:
                    raise KeysUnavailable from exc
            finally:
                self._fetched_at = self._now()  # also after a failure: no retry storm
        if kid not in self._keys:
            if not self._keys:
                raise KeysUnavailable
            raise TokenError("the token was signed with a key the identity provider doesn't list")
        return self._keys[kid]

    async def verify(self, token: str) -> dict[str, Any]:
        """→ the token's claims, or TokenError / KeysUnavailable."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise TokenError("the token is not a valid JWT") from None
        kid = header.get("kid")
        if header.get("alg") not in ALGORITHMS or not isinstance(kid, str):
            raise TokenError("the token's algorithm or key id is not acceptable")
        key = await self._key(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(ALGORITHMS),
                audience=self.audience,
                issuer=self.issuer,
                leeway=LEEWAY_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.ExpiredSignatureError:
            raise TokenError("the token has expired") from None
        except jwt.InvalidAudienceError:
            raise TokenError("the token was not issued for this gateway") from None
        except jwt.InvalidIssuerError:
            raise TokenError(
                "the token was not issued by the configured identity provider"
            ) from None
        except jwt.PyJWTError:
            raise TokenError("the token could not be verified") from None
        return claims
