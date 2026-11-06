"""Provider detection and the one-time registration hint shown in the account form."""

from __future__ import annotations

PROVIDER_BY_DOMAIN = {
    "outlook.com": "microsoft",
    "hotmail.com": "microsoft",
    "live.com": "microsoft",
    "msn.com": "microsoft",
    "gmail.com": "google",
    "googlemail.com": "google",
}
CONSUMER_DOMAINS = {"outlook.com", "hotmail.com", "live.com", "msn.com"}
DEFAULT_HOST = {"microsoft": "outlook.office365.com", "google": "imap.gmail.com"}


def guess_provider(email: str) -> str:
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return PROVIDER_BY_DOMAIN.get(domain, "imap")


def is_consumer(email: str) -> bool:
    return email.rsplit("@", 1)[-1].lower() in CONSUMER_DOMAINS


def registration_hint() -> str:
    return (
        "Microsoft accounts need a one-time application ID (about 3 minutes, free): "
        "portal.azure.com > Microsoft Entra ID > App registrations > New registration; "
        "account types 'Accounts in any organizational directory and personal Microsoft accounts'; "
        "then Authentication > Allow public client flows > Yes. "
        "Paste the Application (client) ID here - it is not a password."
    )
