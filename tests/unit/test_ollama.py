"""Ollama chat client tests — driven by an httpx MockTransport (no network)."""

import json
from collections.abc import Callable

import httpx
import pytest

from expense_analyzer.ollama import LlmVerdict, OllamaClient, OllamaError

_CATEGORIES = [(1, "Groceries"), (2, "Fun")]

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> OllamaClient:
    return OllamaClient(
        base_url="http://piec:11434",
        model="gemma3:12b",
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def _categorize(client: OllamaClient) -> LlmVerdict:
    return client.categorize(
        merchant="BIEDRONKA", description="BIEDRONKA 123", amount=-5000, categories=_CATEGORIES
    )


def test_parses_structured_verdict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        content = json.dumps({"category_id": 2, "confidence": 0.83})
        return httpx.Response(200, json={"message": {"content": content}})

    assert _categorize(_client(handler)) == LlmVerdict(category_id=2, confidence=0.83)


def test_sends_chat_request_with_model_and_schema() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        content = json.dumps({"category_id": 1, "confidence": 0.9})
        return httpx.Response(200, json={"message": {"content": content}})

    _categorize(_client(handler))

    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["model"] == "gemma3:12b"
    assert seen["body"]["stream"] is False
    assert seen["body"]["format"]["required"] == ["category_id", "confidence"]


def test_http_error_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def test_non_json_content_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "not json at all"}})

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def test_missing_fields_raise_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Well-formed JSON but no category_id -> KeyError -> OllamaError.
        return httpx.Response(200, json={"message": {"content": json.dumps({"confidence": 0.5})}})

    with pytest.raises(OllamaError):
        _categorize(_client(handler))


def test_timeout_raises_ollama_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("piec is slow")

    with pytest.raises(OllamaError):
        _categorize(_client(handler))
