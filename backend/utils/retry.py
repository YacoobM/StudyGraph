"""Async retry decorator with exponential backoff."""

import asyncio
from functools import wraps
from typing import Callable, Type
from app.logging_config import get_logger

logger = get_logger(__name__)


def retry_async(
    on: tuple[Type[Exception], ...],
    max_attempts: int = 5,
    backoff: float = 1.0,
):
    """
    Retry an async function on specific exceptions with exponential backoff.
    
    Args:
        on: Tuple of exception types to catch and retry on
        max_attempts: Maximum retry attempts
        backoff: Backoff multiplier (1.0 = no backoff, 2.0 = exponential)
    
    Example:
        @retry_async(on=(RateLimitError,), max_attempts=5, backoff=2.0)
        async def call_api():
            return await some_api_call()
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            attempt = 0
            wait_time = 1.0
            
            while attempt < max_attempts:
                try:
                    return await func(*args, **kwargs)
                except on as e:
                    attempt += 1
                    if attempt >= max_attempts:
                        logger.error(
                            "retry_exhausted func=%s attempts=%d error=%s",
                            func.__name__,
                            attempt,
                            type(e).__name__,
                        )
                        raise
                    
                    logger.warning(
                        "retry_attempt func=%s attempt=%d/%d wait=%.1fs error=%s",
                        func.__name__,
                        attempt,
                        max_attempts,
                        wait_time,
                        type(e).__name__,
                    )
                    await asyncio.sleep(wait_time)
                    wait_time *= backoff
        
        return wrapper
    return decorator
