#!/usr/bin/env python3
"""Owner-scoped persistent settings and public Telegram video links."""

import os
import uuid
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SETTINGS_TABLE = "instagram_auto_video_settings"
LINKS_TABLE = "instagram_auto_video_links"


class AutoVideoStoreError(RuntimeError):
    pass


def auto_video_configured() -> bool:
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)


def _require_config() -> None:
    if not auto_video_configured():
        raise AutoVideoStoreError("Supabase environment variables missing")


def _url(table: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/{table}"


def _headers(prefer: str | None = None) -> dict:
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
    }
    if SUPABASE_SERVICE_KEY.count(".") == 2:
        headers["Authorization"] = f"Bearer {SUPABASE_SERVICE_KEY}"
    if prefer:
        headers["Prefer"] = prefer
    return headers


def get_auto_video_settings(owner_id: str) -> dict:
    _require_config()
    try:
        response = requests.get(
            _url(SETTINGS_TABLE),
            params={
                "select": "owner_id,caption,thumbnail_file_id,updated_at",
                "owner_id": f"eq.{owner_id}",
                "limit": "1",
            },
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoVideoStoreError("Could not load auto-video settings") from exc
    if not isinstance(rows, list):
        raise AutoVideoStoreError("Invalid auto-video settings response")
    if not rows:
        return {
            "owner_id": str(owner_id),
            "caption": "",
            "thumbnail_file_id": "",
        }
    row = rows[0]
    return {
        "owner_id": str(owner_id),
        "caption": str(row.get("caption") or ""),
        "thumbnail_file_id": str(row.get("thumbnail_file_id") or ""),
        "updated_at": row.get("updated_at"),
    }


def update_auto_video_settings(owner_id: str, **updates) -> dict:
    _require_config()
    allowed = {"caption", "thumbnail_file_id"}
    unknown = set(updates) - allowed
    if unknown:
        raise AutoVideoStoreError("Unsupported auto-video setting")
    payload = {
        "owner_id": str(owner_id),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    for name, value in updates.items():
        payload[name] = str(value or "")
    try:
        response = requests.post(
            _url(SETTINGS_TABLE),
            params={"on_conflict": "owner_id"},
            headers=_headers("resolution=merge-duplicates,return=representation"),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoVideoStoreError("Could not save auto-video settings") from exc
    if not isinstance(rows, list) or not rows:
        raise AutoVideoStoreError("Auto-video settings were not saved")
    return rows[0]


def list_auto_video_links(owner_id: str) -> list[dict]:
    _require_config()
    try:
        response = requests.get(
            _url(LINKS_TABLE),
            params={
                "select": "id,telegram_url,channel_username,message_id,position,created_at",
                "owner_id": f"eq.{owner_id}",
                "order": "position.asc,created_at.asc",
            },
            headers=_headers(),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoVideoStoreError("Could not load auto-video links") from exc
    if not isinstance(rows, list):
        raise AutoVideoStoreError("Invalid auto-video links response")
    return rows


def add_auto_video_links(owner_id: str, links: list[dict]) -> list[dict]:
    _require_config()
    existing = list_auto_video_links(owner_id)
    known_urls = {str(row.get("telegram_url") or "") for row in existing}
    next_position = max(
        [int(row.get("position") or 0) for row in existing] or [0]
    ) + 1
    payload = []
    for item in links:
        telegram_url = str(item.get("telegram_url") or "")
        channel_username = str(item.get("channel_username") or "")
        message_id = int(item.get("message_id") or 0)
        if not telegram_url or not channel_username or message_id < 1:
            raise AutoVideoStoreError("Invalid public Telegram video link")
        if telegram_url in known_urls:
            continue
        payload.append({
            "owner_id": str(owner_id),
            "telegram_url": telegram_url,
            "channel_username": channel_username,
            "message_id": message_id,
            "position": next_position,
        })
        known_urls.add(telegram_url)
        next_position += 1
    if not payload:
        return []
    try:
        response = requests.post(
            _url(LINKS_TABLE),
            params={"on_conflict": "owner_id,telegram_url"},
            headers=_headers("resolution=ignore-duplicates,return=representation"),
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoVideoStoreError("Could not add auto-video links") from exc
    if not isinstance(rows, list):
        raise AutoVideoStoreError("Invalid add-links response")
    return rows


def remove_auto_video_links(owner_id: str, record_ids: list[str]) -> list[dict]:
    _require_config()
    safe_ids = []
    for record_id in record_ids:
        try:
            safe_id = str(uuid.UUID(str(record_id)))
        except ValueError as exc:
            raise AutoVideoStoreError("Invalid auto-video link ID") from exc
        if safe_id not in safe_ids:
            safe_ids.append(safe_id)
    if not safe_ids:
        return []
    try:
        response = requests.delete(
            _url(LINKS_TABLE),
            params={
                "owner_id": f"eq.{owner_id}",
                "id": f"in.({','.join(safe_ids)})",
            },
            headers=_headers("return=representation"),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoVideoStoreError("Could not remove auto-video links") from exc
    if not isinstance(rows, list):
        raise AutoVideoStoreError("Invalid remove-links response")
    return rows


def clear_auto_video_links(owner_id: str) -> list[dict]:
    _require_config()
    try:
        response = requests.delete(
            _url(LINKS_TABLE),
            params={"owner_id": f"eq.{owner_id}"},
            headers=_headers("return=representation"),
            timeout=30,
        )
        response.raise_for_status()
        rows = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise AutoVideoStoreError("Could not clear auto-video links") from exc
    if not isinstance(rows, list):
        raise AutoVideoStoreError("Invalid clear-links response")
    return rows
