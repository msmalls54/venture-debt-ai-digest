"""Fail-closed AgentMail delivery boundary."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Self
from urllib.parse import quote

import httpx
from email_validator import EmailNotValidError, validate_email

from vdai.renderer import RenderedEmail

AGENTMAIL_BASE_URL = "https://api.agentmail.to"
MAX_BCC_RECIPIENTS = 49
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._~-]{1,200}$")
_EDITION_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class AgentMailError(RuntimeError):
    """A sanitized delivery configuration, transport, or receipt failure."""


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Secret- and recipient-safe result stored by the send ledger."""

    sent: bool
    status: str
    send_id: str
    recipient_count: int
    message_id: str | None = None
    thread_id: str | None = None
    accepted_at: str | None = None


def build_send_id(edition_date: str, audience: str = "daily") -> str:
    """Build one stable, AgentMail-safe idempotency key per edition/audience."""

    if not _EDITION_DATE.fullmatch(edition_date):
        raise ValueError("edition_date must use YYYY-MM-DD")
    normalized_audience = re.sub(r"[^A-Za-z0-9._~-]+", "-", audience.strip()).strip("-")
    if not normalized_audience:
        raise ValueError("audience must contain an idempotency-safe character")
    key = f"vdai-{edition_date}-{normalized_audience}"[:200]
    if not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError("Could not build a safe idempotency key")
    return key


class AgentMailClient:
    """Send one private-BCC edition, or return a network-free dry-run receipt."""

    def __init__(
        self,
        *,
        api_key: str,
        inbox: str,
        send_enabled: bool,
        test_mode: bool,
        client: httpx.AsyncClient | None = None,
        base_url: str = AGENTMAIL_BASE_URL,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key.strip()
        self._inbox = _normalize_email(inbox)
        self._send_enabled = send_enabled is True
        self._test_mode = test_mode is True
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def deliver(
        self,
        rendered: RenderedEmail,
        *,
        recipients: Sequence[str],
        send_id: str,
    ) -> DeliveryReceipt:
        """Deliver only when both production safety switches explicitly permit it."""

        if not _IDEMPOTENCY_KEY.fullmatch(send_id):
            raise AgentMailError("send_id is not a valid AgentMail idempotency key")
        bcc = _normalize_recipients(recipients, sender=self._inbox)
        if len(bcc) > MAX_BCC_RECIPIENTS:
            raise AgentMailError(
                f"Private BCC delivery is capped at {MAX_BCC_RECIPIENTS} recipients"
            )

        if self._test_mode or not self._send_enabled:
            reason = "DRY_RUN_TEST_MODE" if self._test_mode else "DRY_RUN_SEND_DISABLED"
            return DeliveryReceipt(
                sent=False,
                status=reason,
                send_id=send_id,
                recipient_count=len(bcc),
            )

        if not self._api_key:
            raise AgentMailError("AGENTMAIL_API_KEY is not configured")
        if not bcc:
            raise AgentMailError("At least one private BCC recipient is required for live delivery")
        if not rendered.subject.strip() or not rendered.html.strip() or not rendered.text.strip():
            raise AgentMailError("Subject, HTML, and plain-text bodies are required")

        endpoint = f"{self._base_url}/v0/inboxes/{quote(self._inbox, safe='@')}/messages/send"
        payload = {
            "to": [self._inbox],
            "bcc": bcc,
            "subject": rendered.subject,
            "html": rendered.html,
            "text": rendered.text,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": send_id,
        }
        try:
            response = await self._client.post(endpoint, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise AgentMailError("AgentMail request timed out") from exc
        except httpx.HTTPError as exc:
            raise AgentMailError("AgentMail transport failed") from exc

        if response.status_code != httpx.codes.OK:
            raise AgentMailError(f"AgentMail returned HTTP {response.status_code}")
        try:
            receipt = response.json()
            message_id = str(receipt.get("message_id", "")).strip()
            thread_id = str(receipt.get("thread_id", "")).strip()
        except (TypeError, ValueError) as exc:
            raise AgentMailError("AgentMail returned a malformed receipt") from exc
        if not message_id or not thread_id:
            raise AgentMailError("AgentMail receipt is missing message_id or thread_id")

        return DeliveryReceipt(
            sent=True,
            status="ACCEPTED",
            send_id=send_id,
            recipient_count=len(bcc),
            message_id=message_id,
            thread_id=thread_id,
            accepted_at=datetime.now(UTC).isoformat(),
        )


def _normalize_recipients(recipients: Sequence[str], *, sender: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in recipients:
        address = _normalize_email(str(raw))
        key = address.casefold()
        if key == sender.casefold() or key in seen:
            continue
        seen.add(key)
        normalized.append(address)
    return normalized


def _normalize_email(value: str) -> str:
    try:
        return validate_email(value.strip(), check_deliverability=False).normalized
    except EmailNotValidError as exc:
        raise AgentMailError("An email address is invalid") from exc


__all__ = [
    "AGENTMAIL_BASE_URL",
    "MAX_BCC_RECIPIENTS",
    "AgentMailClient",
    "AgentMailError",
    "DeliveryReceipt",
    "build_send_id",
]
