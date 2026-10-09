import asyncio
import logging
import threading
import time

logger = logging.getLogger(__name__)
from app.config import (
    GRAPH_INPUT_TPM,
    GRAPH_OUTPUT_TPM,
    RAG_INPUT_TPM,
    RAG_OUTPUT_TPM,
)


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


rag_limiter = SharedTokenLimiter(
    workload="rag",
    input_limit=RAG_INPUT_TPM,
    output_limit=RAG_OUTPUT_TPM,
)

graph_limiter = SharedTokenLimiter(
    workload="graph",
    input_limit=GRAPH_INPUT_TPM,
    output_limit=GRAPH_OUTPUT_TPM,
)


def estimate_tokens(text):
    return max(1, len(text) // 4)
