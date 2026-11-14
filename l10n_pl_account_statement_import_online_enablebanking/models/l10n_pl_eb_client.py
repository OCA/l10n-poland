# Copyright 2026 Leszek Szczepkowski
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Minimal Enable Banking API client (no ORM).

Authentication is a JWT signed with the application's RSA private key (RS256),
see https://enablebanking.com/docs/api/reference/ . The token lifetime may not
exceed 24 hours, we use one hour.
"""

import base64
import json
import time
from urllib.parse import quote

import requests

API_URL = "https://api.enablebanking.com"
TIMEOUT = 60
JWT_LIFETIME = 3600


class EnableBankingError(Exception):
    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(application_id, private_key_pem, now=None):
    """Return a RS256 JWT for the Enable Banking API."""
    # Imported here so that a missing library only breaks this provider.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    if not application_id or not private_key_pem:
        raise EnableBankingError("Missing application ID or private key.")
    if isinstance(private_key_pem, str):
        private_key_pem = private_key_pem.encode()
    try:
        key = serialization.load_pem_private_key(private_key_pem.strip(), None)
    except (ValueError, TypeError) as error:
        raise EnableBankingError(f"Invalid private key: {error}") from error
    iat = int(now if now is not None else time.time())
    header = {"typ": "JWT", "alg": "RS256", "kid": application_id}
    payload = {
        "iss": "enablebanking.com",
        "aud": "api.enablebanking.com",
        "iat": iat,
        "exp": iat + JWT_LIFETIME,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


class EnableBankingClient:
    def __init__(self, application_id, private_key_pem, api_url=API_URL):
        self.application_id = application_id
        self.private_key_pem = private_key_pem
        self.api_url = (api_url or API_URL).rstrip("/")

    def _request(self, method, path, params=None, payload=None):
        headers = {
            "Authorization": "Bearer "
            + make_jwt(self.application_id, self.private_key_pem),
            "Accept": "application/json",
        }
        try:
            response = requests.request(
                method,
                self.api_url + path,
                params=params,
                json=payload,
                headers=headers,
                timeout=TIMEOUT,
            )
        except requests.RequestException as error:
            raise EnableBankingError(
                f"Cannot connect to Enable Banking ({self.api_url}): "
                f"{error.__class__.__name__}"
            ) from error
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            message = (
                data.get("message") or data.get("detail") or data.get("error")
                if isinstance(data, dict)
                else None
            ) or response.text[:500]
            raise EnableBankingError(
                f"Enable Banking API error {response.status_code}: {message}",
                status=response.status_code,
                payload=data,
            )
        return data

    # --- Endpoints -----------------------------------------------------------
    def get_application(self):
        return self._request("GET", "/application")

    def get_aspsps(self, country=None, psu_type=None):
        """Banks available through Enable Banking; without a country, the
        banks of every country."""
        params = {}
        if country:
            params["country"] = country
        if psu_type:
            params["psu_type"] = psu_type
        return self._request("GET", "/aspsps", params=params).get("aspsps", [])

    def start_authorization(
        self, aspsp_name, country, valid_until, redirect_url, state, psu_type
    ):
        return self._request(
            "POST",
            "/auth",
            payload={
                "access": {"valid_until": valid_until},
                "aspsp": {"name": aspsp_name, "country": country},
                "state": state,
                "redirect_url": redirect_url,
                "psu_type": psu_type,
            },
        )

    def create_session(self, code):
        return self._request("POST", "/sessions", payload={"code": code})

    def delete_session(self, session_id):
        return self._request("DELETE", f"/sessions/{session_id}")

    def get_account_details(self, account_uid):
        return self._request("GET", f"/accounts/{account_uid}/details")

    def get_balances(self, account_uid):
        data = self._request("GET", f"/accounts/{account_uid}/balances")
        return data.get("balances") or []

    def get_transaction_details(self, account_uid, transaction_id):
        transaction = quote(str(transaction_id), safe="")
        return self._request(
            "GET", f"/accounts/{account_uid}/transactions/{transaction}"
        )

    def get_transactions(self, account_uid, date_from, date_to):
        """Yield booked transactions, following ``continuation_key`` paging."""
        params = {
            "date_from": date_from,
            "date_to": date_to,
            "transaction_status": "BOOK",
        }
        seen_keys = set()
        while True:
            data = self._request(
                "GET", f"/accounts/{account_uid}/transactions", params=params
            )
            yield from data.get("transactions") or []
            key = data.get("continuation_key")
            if not key or key in seen_keys:
                break
            seen_keys.add(key)
            params = dict(params, continuation_key=key)
