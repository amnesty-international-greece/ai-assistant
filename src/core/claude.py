"""LLM client - supports Gemini (prototype) and Claude (production).

Switch providers by setting `llm.provider` in config.yaml:
  provider: gemini   # free tier, for prototyping
  provider: claude   # production
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.config import settings
from src.core.audit import log_action

logger = logging.getLogger(__name__)

# Provider errors worth retrying: overload, rate limits and transient faults.
# Gemini raises errors with ``.code`` (e.g. 503) and status text such as
# "UNAVAILABLE" / "RESOURCE_EXHAUSTED"; Anthropic uses ``.status_code``
# (429, 529 "overloaded") plus timeout / connection errors.
_TRANSIENT_STATUS = frozenset({408, 429, 500, 502, 503, 504, 529})
_TRANSIENT_MARKERS = (
    "unavailable", "resource_exhausted", "overloaded", "high demand",
    "rate limit", "try again later", "timed out", "timeout",
    "deadline exceeded", "connection error",
)


def _is_transient(exc: BaseException) -> bool:
    """True for LLM API errors that usually succeed if simply retried."""
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and value in _TRANSIENT_STATUS:
            return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)

# Pricing per 1M tokens (USD) - used for cost tracking
_PRICING = {
    "claude": {"input": 3.0, "output": 15.0},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
    "gemini-2.5-flash-preview": {"input": 0.15, "output": 0.60},
}


class ClaudeClient:
    """LLM client with provider abstraction.

    Named ClaudeClient for backwards compatibility - all workflows import
    this class unchanged. Internally routes to Gemini or Claude depending
    on config.yaml `llm.provider`.

    Usage:
        client = ClaudeClient()
        text = client.generate(user_prompt="...", system_prompt="...")
    """

    def __init__(self) -> None:
        self._provider = settings.llm.provider
        self._model = settings.llm.model
        self._total_input_tokens = 0
        self._total_output_tokens = 0
        self._backend = self._init_backend()

    def _init_backend(self):
        """Initialise the appropriate SDK client."""
        if self._provider == "gemini":
            from google import genai
            return genai.Client(api_key=settings.gemini_api_key)
        elif self._provider == "claude":
            import anthropic
            return anthropic.Anthropic(api_key=settings.anthropic_api_key)
        else:
            raise ValueError(
                f"Unknown LLM provider: '{self._provider}'. "
                "Set llm.provider to 'gemini' or 'claude' in config.yaml."
            )

    def generate(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        workflow: str = "general",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Send a prompt to the configured LLM and return the text response.

        Args:
            user_prompt: The user message.
            system_prompt: Optional system/instruction prompt.
            workflow: Workflow name for audit logging.
            max_tokens: Override default max tokens.
            temperature: Override default temperature.

        Returns:
            The text content of the model's response.
        """
        max_tokens = max_tokens or settings.llm.max_tokens
        temperature = temperature if temperature is not None else settings.llm.temperature

        log_action(
            workflow=workflow,
            action="llm_request",
            actor="system",
            details={
                "provider": self._provider,
                "model": self._model,
                "prompt_length": len(user_prompt),
            },
        )

        start = time.monotonic()

        text, input_tokens, output_tokens = self._call_with_retry(
            user_prompt, system_prompt, max_tokens, temperature, workflow
        )

        elapsed = time.monotonic() - start
        self._total_input_tokens += input_tokens
        self._total_output_tokens += output_tokens

        log_action(
            workflow=workflow,
            action="llm_response",
            actor=self._provider,
            details={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "elapsed_seconds": round(elapsed, 2),
                "estimated_cost_usd": self._estimate_cost(input_tokens, output_tokens),
            },
        )

        return text

    def _call_with_retry(
        self,
        user_prompt: str,
        system_prompt: str | None,
        max_tokens: int,
        temperature: float,
        workflow: str,
    ) -> tuple[str, int, int]:
        """Call the provider, retrying transient failures with exponential backoff.

        A busy provider ("503 UNAVAILABLE", "529 overloaded", rate limits) used to
        fail the call at once. Callers isolate failures, so that silently left an
        organiser batch unfiled or a drafted block out of the minutes. Errors that
        a retry cannot fix (bad request, auth, invalid model) still raise at once.
        """
        retries = max(0, int(getattr(settings.llm, "max_retries", 4)))
        base = float(getattr(settings.llm, "retry_base_seconds", 5.0))
        call = self._generate_gemini if self._provider == "gemini" else self._generate_claude
        sleep = getattr(self, "_sleep", time.sleep)
        for attempt in range(retries + 1):
            try:
                return call(user_prompt, system_prompt, max_tokens, temperature)
            except Exception as exc:  # noqa: BLE001 - classified below
                if attempt >= retries or not _is_transient(exc):
                    raise
                delay = base * (2 ** attempt)
                logger.warning(
                    "LLM call for %s failed (%s); retry %d/%d in %.0fs",
                    workflow, str(exc)[:160], attempt + 1, retries, delay,
                )
                sleep(delay)
        raise RuntimeError("unreachable")  # pragma: no cover

    def _generate_gemini(
        self,
        user_prompt: str,
        system_prompt: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, int, int]:
        """Call the Gemini API and return (text, input_tokens, output_tokens)."""
        from google.genai import types

        config_kwargs: dict = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt

        response = self._backend.models.generate_content(
            model=self._model,
            contents=user_prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )

        text = response.text

        # Token counts from usage_metadata
        meta = response.usage_metadata
        input_tokens = getattr(meta, "prompt_token_count", 0) or 0
        output_tokens = getattr(meta, "candidates_token_count", 0) or 0

        return text, input_tokens, output_tokens

    def _generate_claude(
        self,
        user_prompt: str,
        system_prompt: str | None,
        max_tokens: int,
        temperature: float,
    ) -> tuple[str, int, int]:
        """Call the Claude API and return (text, input_tokens, output_tokens)."""
        import anthropic

        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user_prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        try:
            response = self._backend.messages.create(**kwargs)
        except anthropic.APIError as e:
            log_action(
                workflow="llm",
                action="llm_error",
                actor="system",
                details={"error": str(e)},
                status="failure",
            )
            raise

        text = response.content[0].text
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        return text, input_tokens, output_tokens

    def load_prompt(self, prompt_name: str) -> str:
        """Load a system prompt from src/prompts/{name}.md.

        Path is configurable via ``settings.storage.prompts_dir`` (default
        ``src/prompts``) - the prompts live with the code now, not in
        ``data/``, since they're code-like artifacts versioned together
        with the workflows that consume them.
        """
        prompts_dir = Path(settings.storage.prompts_dir)
        prompt_path = prompts_dir / f"{prompt_name}.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt not found: {prompt_path}")
        return prompt_path.read_text(encoding="utf-8")

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        pricing = _PRICING.get(self._model, {"input": 0.0, "output": 0.0})
        cost = (input_tokens / 1_000_000 * pricing["input"]) + (
            output_tokens / 1_000_000 * pricing["output"]
        )
        return round(cost, 6)

    @property
    def total_cost(self) -> float:
        return self._estimate_cost(self._total_input_tokens, self._total_output_tokens)

    @property
    def usage_summary(self) -> dict[str, Any]:
        return {
            "provider": self._provider,
            "model": self._model,
            "total_input_tokens": self._total_input_tokens,
            "total_output_tokens": self._total_output_tokens,
            "estimated_total_cost_usd": self.total_cost,
        }
