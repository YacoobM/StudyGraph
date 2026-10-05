"""OpenAI client construction and exception groups."""

from openai import APIStatusError as OpenAIAPIStatusError
from openai import BadRequestError as OpenAIBadRequestError
from openai import OpenAI, RateLimitError as OpenAIRateLimitError

RATE_LIMIT_ERRORS = (OpenAIRateLimitError,)
BAD_REQUEST_ERRORS = (OpenAIBadRequestError,)
API_STATUS_ERRORS = (OpenAIAPIStatusError,)


def create_llm_client(api_key: str, *, timeout=None, max_retries=0):
    kwargs = {"api_key": api_key, "max_retries": max_retries}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return OpenAI(**kwargs)
