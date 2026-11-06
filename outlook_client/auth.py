"""Microsoft identity: device-code flow with a client ID you own.

This deliberately does not reuse any pre-existing application ID. Register your own
(Entra portal -> App registrations -> New -> "Accounts in any org directory and
personal Microsoft accounts" -> Authentication -> Allow public client flows) and put
the client ID in the config file.

Tokens are cached under %LOCALAPPDATA%\\outlook-desktop\\ and refreshed with the stored
refresh token, so the browser step happens once.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AUTHORITY = "https://login.microsoftonline.com"
IMAP_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"


def cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(base) / "outlook-desktop"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _post(url: str, data: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors="replace")
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            raise RuntimeError(f"identity endpoint returned {exc.code}: {payload}") from exc


class DeviceCodeAuth:
    def __init__(self, client_id: str, user: str, tenant: str = "common") -> None:
        if not client_id or not client_id.strip():
            raise ValueError("client_id is required")
        self.client_id = client_id.strip()
        self.user = user
        self.tenant = tenant
        self.token_path = cache_dir() / f"token-{_slug(user)}.json"

    # -- cache --------------------------------------------------------------
    def load_cached(self) -> str | None:
        if not self.token_path.exists():
            return None
        try:
            blob = json.loads(self.token_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if blob.get("expires_at", 0) > time.time() + 60:
            return blob.get("access_token")
        refreshed = self._refresh(blob.get("refresh_token"))
        return refreshed

    def _save(self, payload: dict) -> str:
        payload = dict(payload)
        payload["expires_at"] = time.time() + int(payload.get("expires_in", 3600))
        self.token_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload["access_token"]

    def _refresh(self, refresh_token: str | None) -> str | None:
        if not refresh_token:
            return None
        payload = _post(
            f"{AUTHORITY}/{self.tenant}/oauth2/v2.0/token",
            {
                "client_id": self.client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": IMAP_SCOPE,
            },
        )
        if "access_token" not in payload:
            return None
        return self._save(payload)

    # -- interactive --------------------------------------------------------
    def acquire(self, *, interactive: bool = True, on_code=None) -> str:
        cached = self.load_cached()
        if cached:
            return cached
        if not interactive:
            raise RuntimeError("no cached token; run without --no-browser to sign in")

        start = _post(
            f"{AUTHORITY}/{self.tenant}/oauth2/v2.0/devicecode",
            {"client_id": self.client_id, "scope": IMAP_SCOPE},
        )
        if "device_code" not in start:
            raise RuntimeError(f"device code request failed: {start}")

        if on_code is not None:
            on_code(start)
        else:
            print()
            print("  " + "-" * 62)
            print("  Sign in: " + start.get("verification_uri", "https://microsoft.com/devicelogin"))
            print("  Code:    " + start.get("user_code", "?"))
            print("  " + "-" * 62)
            print()

        interval = int(start.get("interval", 5))
        deadline = time.time() + int(start.get("expires_in", 900))
        while time.time() < deadline:
            time.sleep(interval)
            payload = _post(
                f"{AUTHORITY}/{self.tenant}/oauth2/v2.0/token",
                {
                    "client_id": self.client_id,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": start["device_code"],
                },
            )
            if "access_token" in payload:
                return self._save(payload)
            error = payload.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            if error in {"authorization_declined", "expired_token", "bad_verification_code"}:
                raise RuntimeError(f"sign-in failed: {error}")
            raise RuntimeError(f"token endpoint error: {payload}")
        raise TimeoutError("device code expired before sign-in completed")


def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-.@" else "_" for ch in text)
