"""Gemini calls with retries (rate limits, 5xx, timeouts) and a fallback model."""

from __future__ import annotations

import time
import warnings
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_google_genai import ChatGoogleGenerativeAI
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from retail_analyst.config import Settings
from retail_analyst.observability import Tracer

# Gemini 3 models ignore temperature; we still pass 0 for a fallback model that honours it.
warnings.filterwarnings("ignore", message=r".*uses fixed sampling defaults.*")

_TRANSIENT_MARKERS = ("429", "resource_exhausted", "rate limit", "500", "502", "503", "504",
                      "unavailable", "overloaded", "deadline", "timeout", "timed out")


class LLMUnavailable(Exception):
    """Both the primary and the fallback model failed."""


def is_transient(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    if isinstance(code, int):
        return code == 429 or code >= 500
    text = f"{type(error).__name__} {error}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class GeminiClient:
    def __init__(self, settings: Settings, tools: list[BaseTool], tracer: Tracer) -> None:
        self._tracer = tracer
        self._models = [
            (name, ChatGoogleGenerativeAI(model=name, google_api_key=settings.google_api_key,
                                          temperature=0, timeout=60, max_retries=0)
             .bind_tools(tools))
            for name in (settings.model, settings.fallback_model)
        ]

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        last_error: Exception | None = None
        for index, (name, model) in enumerate(self._models):
            if index > 0:
                self._tracer.event("fallback", model=name, reason=str(last_error)[:200])
            try:
                return self._invoke_with_retries(name, model, messages)
            except Exception as error:  # noqa: BLE001 - try the next model
                last_error = error
                self._tracer.event("llm_error", model=name, error=str(error)[:300])
        raise LLMUnavailable("The language model is not available right now.") from last_error

    def _invoke_with_retries(self, name: str, model: Any,
                             messages: list[BaseMessage]) -> AIMessage:
        for attempt in Retrying(stop=stop_after_attempt(3),
                                wait=wait_exponential_jitter(initial=1, max=10),
                                retry=retry_if_exception(is_transient), reraise=True,
                                before_sleep=lambda s: self._tracer.event(
                                    "retry", service="gemini", model=name,
                                    attempt=s.attempt_number,
                                    error=str(s.outcome.exception())[:200])):
            with attempt:
                started = time.perf_counter()
                message = model.invoke(messages)
                usage = message.usage_metadata or {}
                self._tracer.event("llm_call", model=name,
                                   input_tokens=usage.get("input_tokens", 0),
                                   output_tokens=usage.get("output_tokens", 0),
                                   latency_s=round(time.perf_counter() - started, 2))
                return message
        raise AssertionError("unreachable")
