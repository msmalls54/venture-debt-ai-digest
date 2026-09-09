"""Minimal, fail-closed OpenRouter JSON client.

The client deliberately exposes no model override: the launch model is pinned so a
configuration typo cannot silently switch providers or weaken structured output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from hashlib import sha256
from types import TracebackType
from typing import Any, Literal, Self

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "google/gemini-3.8-flash"
_SUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$id",
        "$defs",
        "$ref",
        "$anchor",
        "type",
        "format",
        "title",
        "description",
        "enum",
        "items",
        "prefixItems",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "anyOf",
        "oneOf",
        "properties",
        "additionalProperties",
        "required",
        "propertyOrdering",
    }
)


class OpenRouterError(RuntimeError):
    """A sanitized OpenRouter transport or response failure."""


class PromptPrivacyError(ValueError):
    """A value explicitly marked private was found in a model prompt."""


class OpenRouterClient:
    """Call OpenRouter with strict JSON Schema output and excluded reasoning.

    ``client`` is injectable so tests can prove the exact outbound request without
    using the network. The API key is never placed in exceptions or return values.
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        endpoint: str = OPENROUTER_URL,
        timeout_seconds: float = 45.0,
    ) -> None:
        self._api_key = api_key.strip()
        self._endpoint = endpoint
        self._owns_client = client is None
        self._json_object_schemas: set[str] = set()
        self._logical_call_count = 0
        self._request_count = 0
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

    @property
    def logical_call_count(self) -> int:
        """Return the number of model tasks attempted by this worker process."""

        return self._logical_call_count

    @property
    def request_count(self) -> int:
        """Return physical OpenRouter requests, including bounded repair fallbacks."""

        return self._request_count

    async def complete_json(
        self,
        *,
        schema_name: str,
        schema: Mapping[str, Any],
        messages: Sequence[Mapping[str, str]],
        forbidden_prompt_values: Sequence[str] = (),
        reasoning_effort: Literal["low", "high"] = "low",
        web_search: bool = False,
        search_citations: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Return one JSON object or raise without leaking prompt/secret content.

        ``web_search`` enables OpenRouter's bounded model-controlled server tool. It is
        deliberately opt-in so evidence extraction and final editing remain closed-book;
        only the discovery scout can search for additional source URLs.
        """

        if not self._api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is not configured")
        if not schema_name or not schema_name.replace("_", "").isalnum():
            raise ValueError("schema_name must contain only letters, numbers, and underscores")

        normalized_messages = self._validate_messages(messages)
        self._assert_prompt_privacy(normalized_messages, forbidden_prompt_values)
        self._logical_call_count += 1

        provider_schema = _provider_schema(schema)
        schema_fingerprint = sha256(
            json.dumps(provider_schema, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        use_json_object = schema_fingerprint in self._json_object_schemas
        request_messages = (
            _schema_prompt(normalized_messages, provider_schema)
            if use_json_object
            else normalized_messages
        )
        payload = {
            "model": OPENROUTER_MODEL,
            "messages": request_messages,
            "reasoning": {"effort": reasoning_effort, "exclude": True},
            "provider": {"require_parameters": True},
            "response_format": (
                {"type": "json_object"}
                if use_json_object
                else {
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "strict": True,
                        "schema": provider_schema,
                    },
                }
            ),
        }
        if web_search:
            payload["tools"] = [
                {
                    "type": "openrouter:web_search",
                    "parameters": {
                        "engine": "exa",
                        "mode": "deep-lite",
                        "max_results": 5,
                        "max_uses": 1,
                        "max_total_results": 15,
                        "search_context_size": "medium",
                        "excluded_domains": [
                            "reddit.com",
                            "x.com",
                            "twitter.com",
                            "facebook.com",
                            "linkedin.com",
                        ],
                    },
                }
            ]
            # Server tools default to model discretion. Discovery runs require a real
            # search, so do not accept a closed-book answer that happens to validate.
            payload["tool_choice"] = "required"
            payload["max_tool_calls"] = 1
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # A full 15-story high-reasoning draft needs more time than routine evidence
        # extraction. This changes the timeout, not token limits or retry count.
        request_timeout = (
            httpx.Timeout(90.0)
            if web_search
            else httpx.Timeout(120.0) if reasoning_effort == "high" else self._client.timeout
        )
        try:
            self._request_count += 1
            response = await self._client.post(
                self._endpoint,
                headers=headers,
                json=payload,
                timeout=request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise OpenRouterError("OpenRouter request timed out") from exc
        except httpx.HTTPError as exc:
            raise OpenRouterError("OpenRouter transport failed") from exc

        if response.status_code == httpx.codes.BAD_REQUEST and not use_json_object:
            self._json_object_schemas.add(schema_fingerprint)
            payload["messages"] = _schema_prompt(normalized_messages, provider_schema)
            payload["response_format"] = {"type": "json_object"}
            try:
                self._request_count += 1
                response = await self._client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                )
            except httpx.TimeoutException as exc:
                raise OpenRouterError("OpenRouter fallback request timed out") from exc
            except httpx.HTTPError as exc:
                raise OpenRouterError("OpenRouter fallback transport failed") from exc
        if response.status_code != httpx.codes.OK:
            raise OpenRouterError(f"OpenRouter returned HTTP {response.status_code}")

        try:
            parsed = self._parse_response_object(response)
            if search_citations is not None:
                search_citations.extend(self._extract_search_citations(response))
            return parsed
        except OpenRouterError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            # A provider can occasionally return HTTP 200 while violating its own
            # structured-output contract. Retry once with the same schema rendered in
            # the prompt and JSON-object mode; never echo the malformed content back.
            self._json_object_schemas.add(schema_fingerprint)
            payload["messages"] = [
                *_schema_prompt(normalized_messages, provider_schema),
                {
                    "role": "user",
                    "content": (
                        "The prior response was malformed. Return one complete JSON object "
                        "matching the supplied schema exactly. Do not add prose or fences."
                    ),
                },
            ]
            payload["response_format"] = {"type": "json_object"}
            try:
                self._request_count += 1
                response = await self._client.post(
                    self._endpoint,
                    headers=headers,
                    json=payload,
                    timeout=request_timeout,
                )
            except httpx.TimeoutException as exc:
                raise OpenRouterError("OpenRouter malformed-output retry timed out") from exc
            except httpx.HTTPError as exc:
                raise OpenRouterError("OpenRouter malformed-output retry failed") from exc
            if response.status_code != httpx.codes.OK:
                raise OpenRouterError(
                    f"OpenRouter malformed-output retry returned HTTP {response.status_code}"
                ) from None
            try:
                parsed = self._parse_response_object(response)
                if search_citations is not None:
                    search_citations.extend(self._extract_search_citations(response))
                return parsed
            except OpenRouterError:
                raise
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OpenRouterError(
                    "OpenRouter returned malformed structured output after one retry"
                ) from exc

    @classmethod
    def _parse_response_object(cls, response: httpx.Response) -> dict[str, Any]:
        envelope = response.json()
        choice = envelope["choices"][0]
        message = choice["message"]
        if message.get("refusal"):
            raise OpenRouterError("OpenRouter model refused the structured request")
        parsed = cls._parse_content(message["content"])
        if not isinstance(parsed, dict):
            raise TypeError("OpenRouter structured output was not a JSON object")
        return parsed

    @staticmethod
    def _extract_search_citations(response: httpx.Response) -> list[dict[str, str]]:
        citations: list[dict[str, str]] = []
        try:
            annotations = response.json()["choices"][0]["message"].get("annotations", [])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            return citations
        for annotation in annotations:
            if not isinstance(annotation, Mapping) or annotation.get("type") != "url_citation":
                continue
            citation = annotation.get("url_citation")
            if not isinstance(citation, Mapping):
                continue
            url = str(citation.get("url") or "").strip()
            title = str(citation.get("title") or "").strip()
            if url:
                citations.append({"url": url, "title": title})
        return citations

    @staticmethod
    def _validate_messages(
        messages: Sequence[Mapping[str, str]],
    ) -> list[dict[str, str]]:
        if not messages:
            raise ValueError("At least one model message is required")
        allowed_roles = {"system", "user", "assistant"}
        normalized: list[dict[str, str]] = []
        for message in messages:
            role = str(message.get("role", "")).strip()
            content = str(message.get("content", ""))
            if role not in allowed_roles or not content.strip():
                raise ValueError("Model messages require an allowed role and non-empty content")
            normalized.append({"role": role, "content": content})
        return normalized

    @staticmethod
    def _assert_prompt_privacy(
        messages: Sequence[Mapping[str, str]], forbidden_values: Sequence[str]
    ) -> None:
        prompt = "\n".join(message["content"] for message in messages).casefold()
        for value in forbidden_values:
            private_value = str(value).strip().casefold()
            if private_value and private_value in prompt:
                raise PromptPrivacyError("A forbidden private value was found in the model prompt")

    @staticmethod
    def _parse_content(content: Any) -> Any:
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            return json.loads(content)
        if isinstance(content, list):
            text_parts = [
                str(part.get("text", ""))
                for part in content
                if isinstance(part, Mapping) and part.get("type") == "text"
            ]
            return json.loads("".join(text_parts))
        raise TypeError("Unsupported OpenRouter message content")


def _provider_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the provider-supported JSON Schema subset; local validation stays strict."""

    definitions = schema.get("$defs", {})

    def inline(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [inline(item, stack) for item in value]
        if not isinstance(value, Mapping):
            return value
        reference = str(value.get("$ref") or "")
        prefix = "#/$defs/"
        if reference.startswith(prefix):
            name = reference.removeprefix(prefix)
            target = definitions.get(name) if isinstance(definitions, Mapping) else None
            if isinstance(target, Mapping) and name not in stack:
                merged = dict(inline(target, (*stack, name)))
                merged.update(
                    {
                        key: inline(item, stack)
                        for key, item in value.items()
                        if key != "$ref"
                    }
                )
                return merged
        return {
            key: inline(item, stack)
            for key, item in value.items()
            if key != "$defs"
        }

    def clean(value: Any) -> Any:
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        output: dict[str, Any] = {}
        for key, item in value.items():
            if key not in _SUPPORTED_SCHEMA_KEYS:
                continue
            if key in {"properties", "$defs"} and isinstance(item, Mapping):
                output[key] = {str(name): clean(child) for name, child in item.items()}
            else:
                output[key] = clean(item)
        return output

    return clean(inline(schema))


def _schema_prompt(
    messages: Sequence[Mapping[str, str]], schema: Mapping[str, Any]
) -> list[dict[str, str]]:
    contract = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return [
        *(
            {"role": str(message["role"]), "content": str(message["content"])}
            for message in messages
        ),
        {
            "role": "user",
            "content": (
                "Return only one JSON object matching this schema exactly. "
                "Do not use Markdown or add fields: "
                + contract
            ),
        },
    ]
