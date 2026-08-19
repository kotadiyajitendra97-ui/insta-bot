#!/usr/bin/env python3
"""Saved Instagram cookie sessions with legacy encrypted-row support."""

import base64
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SESSION_ENCRYPTION_KEY = os.environ.get("SESSION_ENCRYPTION_KEY", "")
TABLE = "instagram_sessions"
AAD = b"instagram-cookie-session-v1"


class SupabaseStoreError(RuntimeError):
    pass


def supabase_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _require_config() -> None:
    if not supabase_configured():
        raise SupabaseStoreError("Supabase environment variables missing")


def _url() -> str:
    return f"{SUPABASE_URL}/rest/v1/{TABLE}"


def _headers(prefer: str | None = None) -> dict:
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
    }
    # Legacy service_role keys are JWTs and use Bearer auth. New sb_secret_
    # keys authenticate through the apikey header and are not valid JWTs.
    if SUPABASE_SERVICE_KEY.count(".") == 2:
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _aes() -> AESGCM:
    if len(SESSION_ENCRYPTION_KEY) < 16:
        raise SupabaseStoreError(
            "SESSION_ENCRYPTION_KEY is required for legacy encrypted sessions"
        )
    key = hashlib.sha256(SESSION_ENCRYPTION_KEY.encode("utf-8")).digest()
    return AESGCM(key)


def _clean_cookies(cookies: dict) -> dict:
    return {
        str(name): str(value)
        for name, value in cookies.items()
        if name and value is not None
    }


def serialize_cookie_header(cookies: dict) -> str:
    """Store cookies in the same compact format shown by Export Cookie."""
    _require_config()
    clean = _clean_cookies(cookies)
    return "; ".join(
        f"{name}={value}" for name, value in sorted(clean.items())
    )


def parse_cookie_header(value: str) -> dict:
    cookies = {}
    for part in str(value or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, cookie_value = part.split("=", 1)
        name = name.strip()
        if name:
            cookies[name] = cookie_value.strip()
    return _clean_cookies(cookies)


def encrypt_cookies(cookies: dict) -> str:
    _require_config()
    clean = _clean_cookies(cookies)
    plaintext = json.dumps(clean, separators=(",", ":"), sort_keys=True).encode("utf-8")
    nonce = os.urandom(12)
    encrypted = _aes().encrypt(nonce, plaintext, AAD)
    return base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")


def decrypt_cookies(token: str) -> dict:
    _require_config()
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        if len(raw) < 29:
            raise ValueError("short encrypted payload")
        plaintext = _aes().decrypt(raw[:12], raw[12:], AAD)
        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("cookie payload is not an object")
        return {str(k): str(v) for k, v in data.items() if k and v is not None}
    except Exception as exc:
        raise SupabaseStoreError("Saved session could not be decrypted") from exc


def decode_stored_cookies(value: str) -> dict:
    """Read compact headers, previous JSON rows and old encrypted rows."""
    _require_config()
    text = str(value or "").strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("cookie payload is not an object")
            return _clean_cookies(data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SupabaseStoreError("Saved cookie JSON is invalid") from exc
    cookie_markers = (
        "sessionid=",
        "csrftoken=",
        "ds_user_id=",
        "mid=",
        "ig_did=",
        "rur=",
    )
    if any(marker in text.lower() for marker in cookie_markers):
        cookies = parse_cookie_header(text)
        if cookies:
            return cookies
        raise SupabaseStoreError("Saved cookie header is invalid")
    return decrypt_cookies(text)


def save_account(owner_id: str, account: dict, cookies: dict) -> dict:
    _require_config()
    instagram_user_id = str(account.get("id", "")).strip()
    username = str(account.get("username", "")).strip().lstrip("@")
    if not instagram_user_id or instagram_user_id == "unknown":
        raise SupabaseStoreError("Instagram user ID missing")
    if not username:
        username = "unknown"

    payload = {
        "owner_id": str(owner_id),
        "instagram_user_id": instagram_user_id,
        "username": username,
        # Keep the existing TEXT column so no SQL migration is required.
        # New values match the compact name=value; name=value export format.
        "encrypted_cookies": serialize_cookie_header(cookies),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = requests.post(
            _url(),
            params={"on_conflict": "owner_id,instagram_user_id"},
            headers=_headers("resolution=merge-duplicates,return=representation"),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        return rows[0] if isinstance(rows, list) and rows else payload
    except requests.RequestException as exc:
        raise SupabaseStoreError("Could not save account in Supabase") from exc


def list_accounts(owner_id: str) -> list[dict]:
    _require_config()
    try:
        response = requests.get(
            _url(),
            params={
                "select": "id,instagram_user_id,username,updated_at",
                "owner_id": f"eq.{owner_id}",
                "order": "username.asc",
            },
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
        return rows if isinstance(rows, list) else []
    except requests.RequestException as exc:
        raise SupabaseStoreError("Could not load saved accounts") from exc


def load_account(owner_id: str, record_id: str) -> dict:
    _require_config()
    try:
        safe_id = str(uuid.UUID(str(record_id)))
    except ValueError as exc:
        raise SupabaseStoreError("Invalid saved account ID") from exc

    try:
        response = requests.get(
            _url(),
            params={
                "select": "id,instagram_user_id,username,encrypted_cookies",
                "owner_id": f"eq.{owner_id}",
                "id": f"eq.{safe_id}",
                "limit": "1",
            },
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except requests.RequestException as exc:
        raise SupabaseStoreError("Could not load saved account") from exc

    if not isinstance(rows, list) or not rows:
        raise SupabaseStoreError("Saved account not found")
    row = rows[0]
    row["cookies"] = decode_stored_cookies(row.pop("encrypted_cookies", ""))
    return row


def delete_accounts(owner_id: str, record_ids: list[str]) -> list[dict]:
    """Permanently delete selected saved accounts belonging to one owner."""
    _require_config()
    safe_ids = []
    for record_id in record_ids:
        try:
            safe_id = str(uuid.UUID(str(record_id)))
        except ValueError as exc:
            raise SupabaseStoreError("Invalid saved account ID") from exc
        if safe_id not in safe_ids:
            safe_ids.append(safe_id)
    if not safe_ids:
        return []
    try:
        response = requests.delete(
            _url(),
            params={"owner_id": f"eq.{owner_id}", "id": f"in.({','.join(safe_ids)})"},
            headers=_headers("return=representation"),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SupabaseStoreError("Could not delete selected saved accounts") from exc
    if not isinstance(rows, list):
        raise SupabaseStoreError("Invalid bulk delete response")
    return rows


def delete_account(owner_id: str, record_id: str) -> dict:
    """Permanently delete one owner's saved Instagram account."""
    _require_config()
    try:
        safe_id = str(uuid.UUID(str(record_id)))
    except ValueError as exc:
        raise SupabaseStoreError("Invalid saved account ID") from exc

    try:
        response = requests.delete(
            _url(),
            params={
                "owner_id": f"eq.{owner_id}",
                "id": f"eq.{safe_id}",
            },
            headers=_headers("return=representation"),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SupabaseStoreError("Could not delete saved account") from exc

    if not isinstance(rows, list) or not rows:
        raise SupabaseStoreError("Saved account not found")
    return rows[0]
