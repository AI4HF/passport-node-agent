"""
Machine authentication (D3).

The agent's modules are machines, not people, so each one holds a Keycloak *service account* and asks
for a token with the ``client_credentials`` grant. There is no offline token to store and no user to
impersonate: the token's ``azp`` claim names the client, which the Passport resolves to a SoftwareAgent
when it writes the audit log.

A service account joins a study group exactly as a person does, so the Passport's existing per-study
authorization applies to it unchanged.
"""

import logging
import time
from typing import Optional

import requests

from .config import KeycloakConfig

logger = logging.getLogger(__name__)

# Ask for a new token slightly before the current one expires, so a long run does not fail mid-way.
_EXPIRY_MARGIN_SECONDS = 30


class AuthenticationError(RuntimeError):
    """Raised when Keycloak will not issue a token for the configured service account."""


class ServiceAccountTokenProvider:
    """
    Supplies bearer tokens for one service account, refreshing them as they expire.

    :param keycloak: the realm and client credentials to authenticate with
    :param timeout: per-request timeout in seconds
    """

    def __init__(self, keycloak: KeycloakConfig, timeout: int = 60):
        self._keycloak = keycloak
        self._timeout = timeout
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    @property
    def client_id(self) -> str:
        """The client this provider authenticates as - the agent's identity in the audit log."""
        return self._keycloak.client_id

    def token(self) -> str:
        """
        Return a valid bearer token, fetching a new one when the held token is missing or stale.

        :return: the access token
        """
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        return self.refresh()

    def refresh(self) -> str:
        """
        Force a new token, used both on first call and after the Passport answers 401.

        :return: the freshly issued access token
        :raises AuthenticationError: when Keycloak refuses or answers without a token
        """
        response = requests.post(
            self._keycloak.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self._keycloak.client_id,
                "client_secret": self._keycloak.client_secret,
            },
            timeout=self._timeout,
        )

        if response.status_code != 200:
            raise AuthenticationError(
                f"Keycloak refused a token for {self._keycloak.client_id!r}: "
                f"{response.status_code} {response.text[:200]}"
            )

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError(
                f"Keycloak answered without an access_token for {self._keycloak.client_id!r}"
            )

        expires_in = int(payload.get("expires_in", 60))
        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - _EXPIRY_MARGIN_SECONDS, 0)
        logger.debug("Obtained a token for %s, valid %ss", self._keycloak.client_id, expires_in)
        return token
