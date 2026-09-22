import asyncio
import os
import threading
import time

from app.config import LLM_PROVIDER


class SharedTokenLimiter:
    def __init__(self, input_limit=7000, output_limit=1000, window_seconds=60.0):
        self.input_limit = input_limit
        self.output_limit = output_limit
        self.window_seconds = window_seconds
        self._window_started = time.monotonic()
        self._input_used = 0
        self._output_used = 0
        self._sync_lock = threading.Lock()

    def _available_wait(self, input_tokens, output_tokens):
        if input_tokens > self.input_limit or output_tokens > self.output_limit:
            raise ValueError(
                f"LLM reservation exceeds per-minute budget: "
                f"input={input_tokens}/{self.input_limit}, "
                f"output={output_tokens}/{self.output_limit}"
            )
        now = time.monotonic()
        if now - self._window_started >= self.window_seconds:
            self._window_started = now
            self._input_used = 0
            self._output_used = 0

        input_ok = self._input_used + input_tokens <= self.input_limit
        output_ok = self._output_used + output_tokens <= self.output_limit
        if input_ok and output_ok:
            self._input_used += input_tokens
            self._output_used += output_tokens
            return 0.0
        return max(self.window_seconds - (now - self._window_started), 0.1)

    def acquire_sync(self, input_tokens, output_tokens):
        while True:
            with self._sync_lock:
                wait = self._available_wait(input_tokens, output_tokens)
            if wait == 0:
                return
            time.sleep(wait)

    async def acquire(self, input_tokens, output_tokens):
        await asyncio.to_thread(self.acquire_sync, input_tokens, output_tokens)


# The generic LLM_* settings override provider defaults. Groq preserves the
# original conservative 7k input / 1k output budget when selected.
_provider_limits = {
    "openai": ("60000", "12000"),
    "groq": ("7000", "1000"),
}[LLM_PROVIDER]
_DEFAULT_INPUT_LIMIT = int(os.getenv("LLM_INPUT_TOKENS_PER_MINUTE", _provider_limits[0]))
_DEFAULT_OUTPUT_LIMIT = int(os.getenv("LLM_OUTPUT_TOKENS_PER_MINUTE", _provider_limits[1]))
limiter = SharedTokenLimiter(_DEFAULT_INPUT_LIMIT, _DEFAULT_OUTPUT_LIMIT)


def estimate_tokens(text):
    return max(1, len(text) // 4)
