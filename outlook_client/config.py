"""Config loading.

Lookup order: --config, $OUTLOOK_DESKTOP_CONFIG, %LOCALAPPDATA%\\outlook-desktop\\config.json,
./config.json.

A config is a JSON object with an "accounts" array. Two shapes are supported:

  microsoft:  { "provider": "microsoft", "host": "outlook.office365.com",
                "user": "...", "client_id": "...", "tenant": "common" }

  google:     { "provider": "google", "host": "imap.gmail.com",
                "user": "...", "app_password": "..." }
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Account:
    name: str
    provider: str
    host: str
    user: str
    port: int = 993
    client_id: str | None = None
    tenant: str = "common"
    app_password: str | None = None

    @property
    def uses_oauth(self) -> bool:
        return self.provider == "microsoft" or bool(self.client_id)

    def to_dict(self) -> dict:
        out: dict = {"name": self.name, "provider": self.provider, "host": self.host, "user": self.user}
        if self.port != 993:
            out["port"] = self.port
        if self.client_id:
            out["client_id"] = self.client_id
        if self.provider == "microsoft" and self.tenant != "common":
            out["tenant"] = self.tenant
        if self.app_password:
            out["app_password"] = self.app_password
        return out


def config_candidates(explicit: str | None = None) -> list[Path]:
    paths: list[Path] = []
    if explicit:
        paths.append(Path(explicit))
    env = os.environ.get("OUTLOOK_DESKTOP_CONFIG")
    if env:
        paths.append(Path(env))
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    paths.append(Path(base) / "outlook-desktop" / "config.json")
    paths.append(Path.cwd() / "config.json")
    return paths


def resolve_config_path(explicit: str | None = None) -> Path | None:
    for path in config_candidates(explicit):
        if path.is_file():
            return path
    return None


def default_config_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "outlook-desktop" / "config.json"


def write_config(accounts: list[Account], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {"accounts": [account.to_dict() for account in accounts]}
    path.write_text(json.dumps(blob, indent=2) + "\n", encoding="utf-8")
    return path


def _parse_accounts(blob: dict, source: object) -> list[Account]:
    raw_accounts = blob.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise ValueError(f"{source}: config must contain a non-empty 'accounts' array")

    accounts: list[Account] = []
    for index, item in enumerate(raw_accounts, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{source}: account #{index} is not an object")
        missing = [k for k in ("host", "user") if not item.get(k)]
        if missing:
            raise ValueError(f"{source}: account #{index} missing {', '.join(missing)}")
        provider = str(item.get("provider", "microsoft")).lower()
        if provider not in {"microsoft", "google", "imap"}:
            raise ValueError(f"{source}: account #{index}: unknown provider {provider!r}")
        accounts.append(
            Account(
                name=str(item.get("name") or item["user"]),
                provider=provider,
                host=str(item["host"]),
                user=str(item["user"]),
                port=int(item.get("port", 993)),
                client_id=(str(item["client_id"]).strip() if item.get("client_id") else None),
                tenant=str(item.get("tenant", "common")),
                app_password=(str(item["app_password"]) if item.get("app_password") else None),
            )
        )
    return accounts


def read_config_file(path: Path, *, missing_ok: bool = False) -> list[Account]:
    if not path.is_file():
        if missing_ok:
            return []
        raise FileNotFoundError(f"no config file at {path}")
    return _parse_accounts(json.loads(path.read_text(encoding="utf-8")), path)


def load_config(explicit: str | None = None, *, missing_ok: bool = False) -> list[Account]:
    # An explicit path means exactly that path: it must not silently fall back to
    # another candidate, or a wizard run with --config would edit the wrong file.
    if explicit:
        return read_config_file(Path(explicit), missing_ok=missing_ok)

    path = resolve_config_path()
    if path is None:
        if missing_ok:
            return []
        tried = ", ".join(str(p) for p in config_candidates())
        raise FileNotFoundError(f"no config found; tried: {tried}")
    return read_config_file(path)
