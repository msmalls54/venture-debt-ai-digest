from __future__ import annotations

import json

import httpx
import pytest

from vdai.openrouter import (
    OPENROUTER_MODEL,
    OpenRouterClient,
    OpenRouterError,
    PromptPrivacyError,
)


@pytest.mark.asyncio
async def test_openrouter_request_pins_reasoning_schema_and_provider_parameters() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        client = OpenRouterClient("secret-key", client=http)
        result = await client.complete_json(
            schema_name="canary_schema",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            messages=[{"role": "user", "content": "Return the test object."}],
        )

    assert result == {"ok": True}
    assert client.logical_call_count == 1
    assert client.request_count == 1
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == OPENROUTER_MODEL == "google/gemini-3.8-flash"
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert body["provider"] == {"require_parameters": True}
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "canary_schema"
    assert captured["headers"]["authorization"] == "Bearer secret-key"


@pytest.mark.asyncio
async def test_openrouter_allows_explicit_high_reasoning_for_editorial_work() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"ok":true}'}}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await OpenRouterClient("secret-key", client=http).complete_json(
            schema_name="editorial_reasoning",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
            messages=[{"role": "user", "content": "Return the editorial object."}],
            reasoning_effort="high",
        )

    assert captured["body"]["reasoning"] == {"effort": "high", "exclude": True}


@pytest.mark.asyncio
async def test_openrouter_removes_unsupported_provider_schema_keywords() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":"yes"}'}}]})

    original = {
        "type": "object",
        "properties": {
            "ok": {
                "type": "string",
                "minLength": 1,
                "maxLength": 10,
            }
        },
        "required": ["ok"],
        "additionalProperties": False,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OpenRouterClient("secret-key", client=http).complete_json(
            schema_name="provider_subset",
            schema=original,
            messages=[{"role": "user", "content": "Return the object."}],
        )

    sent = captured["body"]["response_format"]["json_schema"]["schema"]
    assert result == {"ok": "yes"}
    assert sent["properties"]["ok"] == {"type": "string"}
    assert original["properties"]["ok"]["minLength"] == 1


@pytest.mark.asyncio
async def test_openrouter_inlines_local_schema_definitions_for_provider_compatibility() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"item":{}}'}}]})

    schema = {
        "$defs": {
            "Item": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
        },
        "type": "object",
        "properties": {"item": {"$ref": "#/$defs/Item"}},
        "required": ["item"],
        "additionalProperties": False,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await OpenRouterClient("secret-key", client=http).complete_json(
            schema_name="inline_defs",
            schema=schema,
            messages=[{"role": "user", "content": "Return the object."}],
        )

    sent = captured["body"]["response_format"]["json_schema"]["schema"]
    assert "$defs" not in sent
    assert sent["properties"]["item"]["type"] == "object"


@pytest.mark.asyncio
async def test_openrouter_blocks_explicit_private_values_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient("secret-key", client=http)
        with pytest.raises(PromptPrivacyError, match="forbidden private value"):
            await client.complete_json(
                schema_name="privacy",
                schema={"type": "object"},
                messages=[{"role": "user", "content": "Send this to owner@example.com"}],
                forbidden_prompt_values=["owner@example.com"],
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_openrouter_error_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="secret prompt and provider details")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient("do-not-leak", client=http)
        with pytest.raises(OpenRouterError) as raised:
            await client.complete_json(
                schema_name="failure",
                schema={"type": "object"},
                messages=[{"role": "user", "content": "private prompt"}],
            )

    message = str(raised.value)
    assert "401" in message
    assert "do-not-leak" not in message
    assert "private prompt" not in message
    assert "provider details" not in message


@pytest.mark.asyncio
async def test_openrouter_caches_json_object_fallback_after_provider_schema_rejection() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "invalid schema"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient("secret-key", client=http)
        schema = {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        first = await client.complete_json(
            schema_name="fallback",
            schema=schema,
            messages=[{"role": "user", "content": "Return the object."}],
        )
        second = await client.complete_json(
            schema_name="fallback_repair",
            schema=schema,
            messages=[{"role": "user", "content": "Return the object again."}],
        )

    assert first == second == {"ok": True}
    assert client.logical_call_count == 2
    assert client.request_count == 3
    assert [request["response_format"]["type"] for request in requests] == [
        "json_schema",
        "json_object",
        "json_object",
    ]
    assert "matching this schema exactly" in requests[1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_openrouter_retries_malformed_success_once_without_echoing_bad_content() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        content = '{bad-json' if len(requests) == 1 else '{"ok":true}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await OpenRouterClient("secret-key", client=http).complete_json(
            schema_name="malformed_retry",
            schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            messages=[{"role": "user", "content": "Return the object."}],
        )

    assert result == {"ok": True}
    assert len(requests) == 2
    assert requests[1]["response_format"]["type"] == "json_object"
    retry_prompt = "\n".join(message["content"] for message in requests[1]["messages"])
    assert "prior response was malformed" in retry_prompt
    assert "{bad-json" not in retry_prompt


@pytest.mark.asyncio
async def test_openrouter_web_search_uses_bounded_server_tool_only_when_requested() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok":true}'}}]})

    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenRouterClient("secret-key", client=http)
        await client.complete_json(
            schema_name="closed_book",
            schema=schema,
            messages=[{"role": "user", "content": "Return the object."}],
        )
        citations: list[dict[str, str]] = []
        await client.complete_json(
            schema_name="web_research",
            schema=schema,
            messages=[{"role": "user", "content": "Search and return the object."}],
            web_search=True,
            search_citations=citations,
        )

    assert "tools" not in requests[0]
    tool = requests[1]["tools"][0]
    assert tool["type"] == "openrouter:web_search"
    assert tool["parameters"]["max_results"] == 5
    assert tool["parameters"]["max_uses"] == 1
    assert tool["parameters"]["max_total_results"] == 15
    assert tool["parameters"]["engine"] == "exa"
    assert requests[1]["tool_choice"] == "required"
    assert requests[1]["max_tool_calls"] == 1
    assert citations == []
