"""Provider-neutral client construction and exception groups."""

from groq import APIStatusError as GroqAPIStatusError
from groq import BadRequestError as GroqBadRequestError
from groq import Groq, RateLimitError as GroqRateLimitError
from openai import APIStatusError as OpenAIAPIStatusError
from openai import BadRequestError as OpenAIBadRequestError
from openai import OpenAI, RateLimitError as OpenAIRateLimitError

from app.config import LLM_PROVIDER


RATE_LIMIT_ERRORS = (GroqRateLimitError, OpenAIRateLimitError)
BAD_REQUEST_ERRORS = (GroqBadRequestError, OpenAIBadRequestError)
API_STATUS_ERRORS = (GroqAPIStatusError, OpenAIAPIStatusError)


def create_llm_client(api_key: str, *, timeout=None, max_retries=0):
    kwargs = {"api_key": api_key, "max_retries": max_retries}
    if timeout is not None:
        kwargs["timeout"] = timeout
    if LLM_PROVIDER == "groq":
        return Groq(**kwargs)
    return OpenAI(**kwargs)
