from __future__ import annotations

import json

import httpx
import pytest

from vdai.agentmail import AgentMailClient, AgentMailError, build_send_id
from vdai.renderer import RenderedEmail


def _rendered() -> RenderedEmail:
    return RenderedEmail(
        subject="OpenAI tightens the model-cost race",
        html="<html><body>Digest</body></html>",
        text="Digest",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("test_mode", "send_enabled", "expected_status"),
    [
        (True, True, "DRY_RUN_TEST_MODE"),
        (True, False, "DRY_RUN_TEST_MODE"),
        (False, False, "DRY_RUN_SEND_DISABLED"),
    ],
)
async def test_agentmail_hard_gate_renders_dry_run_without_network(
    test_mode: bool, send_enabled: bool, expected_status: str
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentMailClient(
            api_key="",
            inbox="mikesupdateagent@agentmail.to",
            send_enabled=send_enabled,
            test_mode=test_mode,
            client=http,
        )
        receipt = await client.deliver(
            _rendered(),
            recipients=["reader@example.com"],
            send_id="vdai-2026-08-25-daily",
        )

    assert receipt.sent is False
    assert receipt.status == expected_status
    assert receipt.recipient_count == 1
    assert calls == 0


@pytest.mark.asyncio
async def test_agentmail_live_payload_uses_visible_sender_private_bcc_and_receipt_ids() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "msg_123", "thread_id": "thr_456"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentMailClient(
            api_key="agentmail-secret",
            inbox="mikesupdateagent@agentmail.to",
            send_enabled=True,
            test_mode=False,
            client=http,
        )
        receipt = await client.deliver(
            _rendered(),
            recipients=[
                "Reader@example.com",
                "reader@example.com",
                "second@example.com",
                "mikesupdateagent@agentmail.to",
            ],
            send_id="vdai-2026-08-25-daily",
        )

    assert receipt.sent is True
    assert receipt.message_id == "msg_123"
    assert receipt.thread_id == "thr_456"
    assert receipt.recipient_count == 2
    assert captured["url"].endswith("/v0/inboxes/mikesupdateagent@agentmail.to/messages/send")
    body = captured["body"]
    assert body["to"] == ["mikesupdateagent@agentmail.to"]
    assert body["bcc"] == ["Reader@example.com", "second@example.com"]
    assert "cc" not in body
    assert captured["headers"]["idempotency-key"] == "vdai-2026-08-25-daily"
    assert captured["headers"]["authorization"] == "Bearer agentmail-secret"


@pytest.mark.asyncio
async def test_agentmail_requires_http_200_and_both_provider_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message_id": "msg_only"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentMailClient(
            api_key="secret",
            inbox="mikesupdateagent@agentmail.to",
            send_enabled=True,
            test_mode=False,
            client=http,
        )
        with pytest.raises(AgentMailError, match="message_id or thread_id"):
            await client.deliver(
                _rendered(),
                recipients=["reader@example.com"],
                send_id="vdai-2026-08-25-daily",
            )


@pytest.mark.asyncio
async def test_agentmail_rejects_more_than_49_private_recipients_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"message_id": "m", "thread_id": "t"})

    recipients = [f"reader{index}@example.com" for index in range(50)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = AgentMailClient(
            api_key="secret",
            inbox="mikesupdateagent@agentmail.to",
            send_enabled=True,
            test_mode=False,
            client=http,
        )
        with pytest.raises(AgentMailError, match="capped at 49"):
            await client.deliver(
                _rendered(), recipients=recipients, send_id="vdai-2026-08-25-daily"
            )

    assert calls == 0


def test_build_send_id_is_stable_and_agentmail_safe() -> None:
    first = build_send_id("2026-08-25", "owner group")
    second = build_send_id("2026-08-25", "owner group")

    assert first == second == "vdai-2026-08-25-owner-group"
    assert ":" not in first
    assert "@" not in first
