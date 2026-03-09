from __future__ import annotations

import atexit
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

from langfuse import Langfuse

TRACEPARENT_PATTERN = re.compile(r"^[\da-f]{2}-([\da-f]{32})-([\da-f]{16})-[\da-f]{2}$")
_TRUTHY = {"1", "true", "yes", "on"}
_LANGFUSE_CLIENT: Langfuse | None = None


class _NoopObservation:
    id: str | None = None
    trace_id: str | None = None

    def update(self, **kwargs: Any) -> "_NoopObservation":
        return self

    def end(self, end_time: int | None = None) -> "_NoopObservation":
        return self


_NOOP_OBSERVATION = _NoopObservation()


def _langfuse_enabled() -> bool:
    enabled = os.environ.get("LANGFUSE_ENABLED", "false").strip().lower()
    has_credentials = bool(os.environ.get("LANGFUSE_PUBLIC_KEY")) and bool(os.environ.get("LANGFUSE_SECRET_KEY"))
    return enabled in _TRUTHY and has_credentials


def get_langfuse_client() -> Langfuse | None:
    global _LANGFUSE_CLIENT
    if _LANGFUSE_CLIENT is None and _langfuse_enabled():
        _LANGFUSE_CLIENT = Langfuse(
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY"),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY"),
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            environment=os.environ.get("LANGFUSE_ENVIRONMENT"),
            release=os.environ.get("MCP_SERVER_RELEASE"),
        )
    return _LANGFUSE_CLIENT


@atexit.register
def _flush_langfuse() -> None:
    client = get_langfuse_client()
    if client is not None:
        client.flush()


def request_headers(ctx: Any | None) -> dict[str, str]:
    if ctx is None:
        return {}

    try:
        request = ctx.request_context.request
    except Exception:
        return {}

    if request is None or not hasattr(request, "headers"):
        return {}

    return {str(key).lower(): str(value) for key, value in request.headers.items()}


def request_metadata(ctx: Any | None) -> dict[str, Any]:
    headers = request_headers(ctx)
    metadata: dict[str, Any] = {
        "service": "mcp-server",
        "component": "chatbi-mcp",
    }

    mapping = {
        "conversation_id": headers.get("x-chatbi-conversation-id"),
        "project_id": headers.get("x-chatbi-project-id"),
        "user_id": headers.get("x-chatbi-user-id"),
        "chat_mode": headers.get("x-chatbi-chat-mode"),
        "session_id": headers.get("x-chatbi-session-id"),
    }

    metadata.update({key: value for key, value in mapping.items() if value not in (None, "")})
    return metadata


def trace_context_from_headers(headers: dict[str, str]) -> dict[str, str] | None:
    traceparent = headers.get("traceparent")
    if not traceparent:
        return None

    match = TRACEPARENT_PATTERN.match(traceparent.strip().lower())
    if not match:
        return None

    trace_id, parent_span_id = match.groups()
    return {
        "trace_id": trace_id,
        "parent_span_id": parent_span_id,
    }


@contextmanager
def start_observation(
    *,
    name: str,
    as_type: str = "span",
    input_payload: Any = None,
    output_payload: Any = None,
    metadata: dict[str, Any] | None = None,
    ctx: Any | None = None,
    model: str | None = None,
) -> Iterator[Any]:
    client = get_langfuse_client()
    if client is None:
        yield _NOOP_OBSERVATION
        return

    headers = request_headers(ctx)
    merged_metadata = request_metadata(ctx)
    if metadata:
        merged_metadata.update(metadata)

    kwargs: dict[str, Any] = {
        "name": name,
        "as_type": as_type,
        "input": input_payload,
        "output": output_payload,
        "metadata": merged_metadata or None,
    }
    if model:
        kwargs["model"] = model

    trace_context = trace_context_from_headers(headers)
    if trace_context:
        kwargs["trace_context"] = trace_context

    with client.start_as_current_observation(**kwargs) as observation:
        yield observation
