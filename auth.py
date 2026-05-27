import os
import json
import time
import functools
import urllib.request

import jwt
from flask import request, jsonify, g

_jwks_cache: dict = {}
_jwks_expiry: float = 0.0


def _get_jwks() -> dict:
    """Fetch and cache JWKS from Neon Auth. Re-fetches every 60 minutes."""
    global _jwks_cache, _jwks_expiry
    if _jwks_cache and time.time() < _jwks_expiry:
        return _jwks_cache

    url = os.environ["NEON_AUTH_JWKS_URL"]
    with urllib.request.urlopen(url, timeout=10) as resp:
        _jwks_cache = json.loads(resp.read().decode())
    _jwks_expiry = time.time() + 3600
    return _jwks_cache


def _get_public_key(kid: str):
    """Find the public key (Ed25519 or RSA) matching the token's kid."""
    jwks = _get_jwks()
    for key_data in jwks.get("keys", []):
        if key_data.get("kid") == kid:
            # PyJWK handles EdDSA/RSA/EC generically
            return jwt.PyJWK(key_data).key
    # If no kid match (e.g., single-key JWKS without explicit kid), fall back to first key
    keys = jwks.get("keys", [])
    if keys:
        return jwt.PyJWK(keys[0]).key
    raise ValueError(f"No matching key found for kid={kid!r}")


def _auth_origin() -> str:
    """Return scheme+host of NEON_AUTH_BASE_URL (the JWT aud/iss claim value)."""
    from urllib.parse import urlparse
    p = urlparse(os.environ["NEON_AUTH_BASE_URL"])
    return f"{p.scheme}://{p.netloc}"


def verify_token(token: str) -> dict:
    """
    Decode and verify a Neon Auth (Better Auth) JWT.
    Returns the decoded payload (contains 'sub' = user_id).
    """
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    public_key = _get_public_key(kid)
    origin = _auth_origin()
    # Neon Auth sets aud and iss to the origin (scheme + host, no path)
    return jwt.decode(
        token,
        public_key,
        algorithms=["EdDSA", "RS256"],
        audience=origin,
        issuer=origin,
        options={"verify_exp": True},
    )


def require_auth(f):
    """
    Flask route decorator. Sets g.user_id on success; returns 401 on failure.

    Usage:
        @app.route("/api/songs")
        @require_auth
        def get_songs():
            user_id = g.user_id
    """
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401
        token = auth_header[len("Bearer "):]
        try:
            payload = verify_token(token)
            g.user_id = payload["sub"]
        except Exception as e:
            return jsonify({"error": f"Invalid token: {e}"}), 401
        return f(*args, **kwargs)
    return wrapper
