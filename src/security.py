"""Authentication, approval challenges, execution capabilities, webhook
signatures, and rate limiting.

Two different secrets protect a state change:

1. **Approval challenge** - shown only to an authenticated approver on the
   approval page. It proves the decision came from that page (it doubles as a
   CSRF token) and is scoped to one action and one purpose (`approve` or
   `rollback`). It is derived with HMAC from a per-action nonce, so the raw
   value is never stored and clearing the nonce invalidates it immediately.

2. **Execution capability** - minted by the server after a valid human
   approval. It is bound to the action ID, exact tool, normalized arguments,
   target service, approver, expiry time, and a random nonce. Only its SHA-256
   hash is stored. Every action tool (HTTP, worker, or MCP) must spend one,
   and spending is a single atomic database update, so it works once.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any

from .config import settings
from .policy import args_hash
from .state import store


class ApprovalError(PermissionError):
    """Raised when an action is attempted without a valid approval capability."""


# ---------------------------------------------------------------- challenges
def new_nonce() -> str:
    return secrets.token_urlsafe(18)


def challenge_token(scope: str, action_id: str, nonce: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), f"{scope}:{action_id}:{nonce}".encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")


def new_challenge(scope: str, ttl_seconds: int | None = None) -> dict:
    return {
        "scope": scope,
        "nonce": new_nonce(),
        "expires_at": time.time() + (ttl_seconds or settings.approval_ttl_seconds),
    }


def current_challenge_token(action: dict, scope: str) -> str | None:
    ch = action.get("challenge")
    if not ch or ch.get("scope") != scope:
        return None
    return challenge_token(scope, action["id"], ch["nonce"])


def challenge_expired(action: dict) -> bool:
    ch = action.get("challenge")
    return ch is not None and time.time() >= float(ch["expires_at"])


def verify_challenge(action: dict, scope: str, presented: str) -> bool:
    expected = current_challenge_token(action, scope)
    if not expected or not presented or challenge_expired(action):
        return False
    return hmac.compare_digest(expected.encode(), presented.encode())


# -------------------------------------------------------------- capabilities
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint_capability(*, action_id: str, scope: str, tool: str, args: dict, service: str, approver: str) -> str:
    token = "cap_" + secrets.token_urlsafe(24)
    now = time.time()
    store.insert_capability(
        token_hash=hash_token(token),
        action_id=action_id,
        scope=scope,
        tool=tool,
        args_hash=args_hash(tool, args) if scope == "execute" else hash_token(f"rollback:{action_id}"),
        service=service,
        approver=approver,
        created_at=now,
        expires_at=now + settings.capability_ttl_seconds,
    )
    return token


def check_capability(
    token: str | None,
    *,
    scope: str,
    tool: str,
    args: dict,
    service: str,
    action_id: str | None = None,
) -> dict:
    """Validate a capability WITHOUT consuming it. Raises ApprovalError."""
    if not token:
        raise ApprovalError("missing approval capability - refusing to act")
    cap = store.get_capability(hash_token(token))
    if cap is None:
        raise ApprovalError("unknown approval capability - refusing to act")
    expected_args = args_hash(tool, args) if scope == "execute" else hash_token(f"rollback:{cap['action_id']}")
    mismatches = [
        name
        for name, ok in (
            ("scope", cap["scope"] == scope),
            ("tool", cap["tool"] == tool),
            ("arguments", hmac.compare_digest(cap["args_hash"], expected_args)),
            ("service", cap["service"] == service),
            ("action", action_id is None or cap["action_id"] == action_id),
        )
        if not ok
    ]
    if mismatches:
        raise ApprovalError(f"capability does not authorize this call (mismatch: {', '.join(mismatches)})")
    now = time.time()
    if cap["used_at"] is not None:
        raise ApprovalError("capability already used - replay refused")
    if now >= cap["expires_at"]:
        raise ApprovalError("capability expired")
    return cap


def spend_capability(
    token: str | None,
    *,
    scope: str,
    tool: str,
    args: dict,
    service: str,
    action_id: str | None = None,
) -> dict:
    """Validate and atomically consume a capability. Raises ApprovalError."""
    cap = check_capability(token, scope=scope, tool=tool, args=args, service=service, action_id=action_id)
    if not store.mark_capability_used(cap["token_hash"], time.time()):
        raise ApprovalError("capability already used or expired")
    return cap


# -------------------------------------------------------------------- users
_DEV_USERS = "demo:demo"


def _parse_users(raw: str) -> dict[str, str]:
    users: dict[str, str] = {}
    for pair in raw.split(","):
        if ":" in pair:
            name, _, secret = pair.strip().partition(":")
            if name and secret:
                users[name] = secret
    return users


def configured_users() -> dict[str, str]:
    raw = settings.users or ("" if settings.is_production else _DEV_USERS)
    return _parse_users(raw)


def check_password(username: str, password: str) -> bool:
    stored = configured_users().get(username)
    if stored is None:
        # constant-time-ish: still do a comparison so timing does not reveal users
        hmac.compare_digest(b"x" * 32, password.encode()[:32].ljust(32, b"y"))
        return False
    if stored.startswith("sha256:"):
        digest = hashlib.sha256(password.encode()).hexdigest()
        return hmac.compare_digest(stored[len("sha256:") :], digest)
    return hmac.compare_digest(stored.encode(), password.encode())


# ----------------------------------------------------------------- webhooks
def sign_body(body: bytes, secret: str | None = None) -> str:
    key = (secret or settings.webhook_secret).encode()
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def verify_webhook_signature(body: bytes, header: str | None) -> bool:
    if not settings.webhook_secret:
        # Allowed only outside production (startup validation enforces this).
        return not settings.is_production
    if not header:
        return False
    return hmac.compare_digest(sign_body(body), header.strip())


# ------------------------------------------------------------- rate limiting
class RateLimiter:
    """Sliding-window limiter keyed by client and route group.

    Per process. Behind several replicas, put the platform's rate limiting
    (or a shared store such as Redis) in front as well.
    """

    def __init__(self) -> None:
        self._hits: dict[Any, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: Any, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()
