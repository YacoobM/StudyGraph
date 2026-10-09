"""User authentication and session management."""


from dataclasses import dataclass
from typing import TypedDict
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json

from fastapi import Header, HTTPException

from app.config import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL
from app.logging_config import get_logger


logger = get_logger(__name__)


# === Type Definitions ===

class SupabaseUser(TypedDict, total=False):
    """Supabase user object returned from /auth/v1/user."""
    id: str
    email: str
    user_metadata: dict

# === Supabase Integration ===
def _supabase_user(token: str) -> SupabaseUser:
    """
    Fetch the authenticated user from Supabase using a Bearer token.
    
    Args:
        token: The Supabase auth token (JWT).
    
    Returns:
        A SupabaseUser dict with id, email, and metadata.
    
    Raises:
        HTTPException(401): If the token is invalid or Supabase request fails.
        HTTPException(500): If Supabase is not configured.
    """
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        logger.error("supabase_not_configured")
        raise HTTPException(
            status_code=500,
            detail="Authentication service unavailable.",
        )

    request = Request(
        f"{SUPABASE_URL}/auth/v1/user",
        headers={
            "Authorization": f"Bearer {token}",
            "apikey": SUPABASE_PUBLISHABLE_KEY,
        },
    )
    
    try:
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read())
            return data
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        logger.warning(
            "supabase_request_failed error_type=%s",
            type(e).__name__,
        )
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    except Exception as e:
        logger.exception("supabase_unexpected_error")
        raise HTTPException(status_code=401, detail="Sign in to continue.")


# === User Lookup & Creation ===

@dataclass(frozen=True)
class SupabaseIdentity:
	auth_user_id: str
	email: str
	access_token: str


def current_supabase_identity(authorization: str = Header(default="")) -> SupabaseIdentity:
	if not authorization.startswith("Bearer "):
		raise HTTPException(status_code=401, detail="Sign in to continue.")

	token = authorization[7:].strip()
	supabase_user = _supabase_user(token)
	auth_user_id = supabase_user.get("id")
	email = (supabase_user.get("email") or "").strip().lower()
	if not auth_user_id or not email:
		raise HTTPException(status_code=401, detail="Supabase returned an incomplete user.")

	return SupabaseIdentity(
		auth_user_id=auth_user_id,
		email=email,
		access_token=token,
	)