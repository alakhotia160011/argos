"""Anthropic API wrapper with retries and structured output parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import anthropic
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.config import Settings

logger = logging.getLogger(__name__)

_settings: Settings | None = None
_client: anthropic.AsyncAnthropic | None = None
_semaphore: asyncio.Semaphore | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        s = _get_settings()
        _client = anthropic.AsyncAnthropic(api_key=s.anthropic_api_key)
    return _client


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_get_settings().llm_concurrency)
    return _semaphore


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((anthropic.RateLimitError, anthropic.APIConnectionError)),
)
async def call_agent(
    system_prompt: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
) -> str:
    """Call Claude with a system prompt and user message. Returns raw text response."""
    client = _get_client()
    s = _get_settings()
    sem = _get_semaphore()

    async with sem:
        logger.debug("LLM call: model=%s, prompt_len=%d", model or s.llm_model, len(user_message))
        response = await client.messages.create(
            model=model or s.llm_model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text
        logger.debug(
            "LLM response: %d chars, input_tokens=%d, output_tokens=%d",
            len(text), response.usage.input_tokens, response.usage.output_tokens,
        )
        return text


async def call_agent_json(
    system_prompt: str,
    user_message: str,
    model: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.3,
) -> dict[str, Any]:
    """Call Claude and parse the response as JSON.

    Extracts the first JSON object found in the response text.
    """
    text = await call_agent(system_prompt, user_message, model, max_tokens, temperature)
    return extract_json(text)


def extract_json(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a text response."""
    # Try parsing the whole response first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Look for JSON in code blocks
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Look for any JSON object
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to extract JSON from response: %s...", text[:200])
    return {"raw_response": text, "_parse_error": True}
