import asyncio
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)
from app.config import LLM_PROVIDER


class SharedTokenLimiter:
    def __init__(
        self, workload, input_limit=7000, output_limit=1000, window_seconds=60.0
    ):
        self.input_limit = input_limit
        self.output_limit = output_limit
        self.window_seconds = window_seconds
        self._window_started = time.monotonic()
        self._input_used = 0
        self._output_used = 0
        self._sync_lock = threading.Lock()
        self.workload = workload

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

    def acquire_sync(self, input_tokens: int, output_tokens: int) -> None:
        """Synchronously acquire tokens. Blocks until the reservation fits, then records it."""
        while True:
            with self._sync_lock:
                wait_time = self._available_wait(input_tokens, output_tokens)
            if wait_time <= 0:
                return
            logger.warning(
                "workload=%s wait_seconds=%.1f input_tokens=%d output_tokens=%d",
                self.workload,
                wait_time,
                input_tokens,
                output_tokens,
            )
            time.sleep(wait_time)

    async def acquire(self, input_tokens: int, output_tokens: int) -> None:
        """Asynchronously acquire tokens. Awaits until the reservation fits, then records it."""
        while True:
            with self._sync_lock:
                wait_time = self._available_wait(input_tokens, output_tokens)
            if wait_time <= 0:
                return
            logger.warning(
                "workload=%s wait_seconds=%.1f input_tokens=%d output_tokens=%d",
                self.workload,
                wait_time,
                input_tokens,
                output_tokens,
            )
            await asyncio.sleep(wait_time)


# The generic LLM_* settings override provider defaults. Groq preserves the
# original conservative 7k input / 1k output budget when selected.
_provider_limits = {
    "openai": ("60000", "12000"),
    "groq": ("7000", "2000"),
}[LLM_PROVIDER]
_DEFAULT_INPUT_LIMIT = int(
    os.getenv("LLM_INPUT_TOKENS_PER_MINUTE", _provider_limits[0])
)
_DEFAULT_OUTPUT_LIMIT = int(
    os.getenv("LLM_OUTPUT_TOKENS_PER_MINUTE", _provider_limits[1])
)

rag_limiter = SharedTokenLimiter(
    workload="rag",
    input_limit=int(os.getenv("RAG_INPUT_TPM", "100000")),
    output_limit=int(os.getenv("RAG_OUTPUT_TPM", "50000")),
)

graph_limiter = SharedTokenLimiter(
    workload="graph",
    input_limit=int(os.getenv("GRAPH_INPUT_TPM", "200000")),
    output_limit=int(os.getenv("GRAPH_INPUT_TPM", "100000")),
)


limiter = SharedTokenLimiter(_DEFAULT_INPUT_LIMIT, _DEFAULT_OUTPUT_LIMIT)

# Interactive RAG traffic gets its own reservation pool so a large extraction
# or fusion batch cannot consume the capacity needed for a user's question.
_RAG_INPUT_LIMIT = int(os.getenv("RAG_INPUT_TOKENS_PER_MINUTE", _DEFAULT_INPUT_LIMIT))
_RAG_OUTPUT_LIMIT = int(os.getenv("RAG_OUTPUT_TOKENS_PER_MINUTE", _DEFAULT_OUTPUT_LIMIT))
rag_limiter = SharedTokenLimiter(_RAG_INPUT_LIMIT, _RAG_OUTPUT_LIMIT)


def estimate_tokens(text):
    return max(1, len(text) // 4)
