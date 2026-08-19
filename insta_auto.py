#!/usr/bin/env python3
"""Instagram cookie-session utilities for the owner-only Telegram bot."""

import argparse
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import requests

BASE_URL = "https://www.instagram.com"
API_URL = f"{BASE_URL}/api"
UPLOAD_URL = "https://i.instagram.com"
IG_APP_ID = "936619743392459"
SESSION_FILE = os.path.join(os.path.expanduser("~"), ".ig_session.json")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/150.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "X-IG-App-ID": IG_APP_ID,
}

COOKIE_NAMES = {
    "sessionid", "csrftoken", "ds_user_id", "mid", "ig_did",
    "datr", "rur", "ig_nrcb", "wd", "dpr",
}


def _safe_cookie_value(value) -> bool:
    value = str(value)
    return bool(value) and len(value) <= 8192 and not any(
        ord(ch) < 32 or ord(ch) == 127 for ch in value
    )


def _cookies_from_export(data) -> dict:
    """Parse browser-extension cookie exports (JSON list or cookies object)."""
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        data = data["cookies"]
    if isinstance(data, dict):
        return {
            str(name): str(value)
            for name, value in data.items()
            if name in COOKIE_NAMES and value is not None and _safe_cookie_value(value)
        }
    if not isinstance(data, list):
        return {}

    cookies = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", "")).strip()
        # Browser-export JSON is fresher and takes priority over any prefix.
        if name in COOKIE_NAMES and name not in cookies and _safe_cookie_value(value):
            cookies[name] = value
    return cookies


def parse_cookie_string(cookie_text: str) -> dict:
    """Parse Cookie header, raw sessionid, or browser-export JSON array."""
    text = (cookie_text or "").strip()
    if not text:
        return {}

    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()

    cookies = {}

    # First try the entire input as JSON.
    if text.startswith(("{", "[")):
        for candidate in (text, text.replace("\\n", "\n").replace('\\"', '"')):
            try:
                cookies.update(_cookies_from_export(json.loads(candidate)))
                if cookies:
                    return cookies
            except (json.JSONDecodeError, TypeError):
                continue

    # Also support accidental input like: sessionid=[{...browser export...}]
    array_start = text.find("[")
    array_end = text.rfind("]")
    pair_text = text
    if array_start >= 0 and array_end > array_start:
        pair_text = text[:array_start]
        array_text = text[array_start:array_end + 1]
        for candidate in (
            array_text,
            array_text.replace("\\n", "\n").replace('\\"', '"'),
        ):
            try:
                cookies.update(_cookies_from_export(json.loads(candidate)))
                break
            except (json.JSONDecodeError, TypeError):
                continue

    # A raw sessionid is also accepted.
    if "=" not in text and ";" not in text:
        value = text.strip('"\'')
        return {"sessionid": value} if _safe_cookie_value(value) else {}

    for part in pair_text.replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"\'')
        # Values extracted from a browser-export JSON array take priority.
        if name in COOKIE_NAMES and name not in cookies and _safe_cookie_value(value):
            cookies[name] = value
    return cookies


def apply_cookies(session: requests.Session, cookies: dict) -> None:
    for name, value in cookies.items():
        if name in COOKIE_NAMES and _safe_cookie_value(value):
            session.cookies.set(name, value, domain=".instagram.com", path="/")

    # ds_user_id is the first field in a decoded sessionid.
    if not session.cookies.get("ds_user_id") and session.cookies.get("sessionid"):
        decoded = unquote(session.cookies.get("sessionid", ""))
        candidate = decoded.split(":", 1)[0]
        if candidate.isdigit():
            session.cookies.set("ds_user_id", candidate, domain=".instagram.com", path="/")


def save_session(session: requests.Session, username: str = "cookie") -> None:
    data = {
        "cookies": dict(session.cookies),
        "username": username,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(SESSION_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass
    print(f"Cookie session saved: {SESSION_FILE}")


def load_session(session: requests.Session) -> bool:
    if not os.path.exists(SESSION_FILE):
        return False
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            data = json.load(f)
        apply_cookies(session, data.get("cookies", {}))
        print(f"Cookie session loaded (saved: {data.get('saved_at', '?')})")
        return bool(session.cookies.get("sessionid"))
    except Exception as exc:
        print(f"Session load failed: {exc}")
        return False


def authenticated_headers(session: requests.Session) -> dict:
    headers = dict(DEFAULT_HEADERS)
    csrf = session.cookies.get("csrftoken", "")
    if csrf:
        headers["X-CSRFToken"] = csrf
    headers["Referer"] = f"{BASE_URL}/"
    headers["Origin"] = BASE_URL
    return headers


def is_logged_in(session: requests.Session) -> bool:
    if not session.cookies.get("sessionid"):
        return False

    headers = authenticated_headers(session)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "X-ASBD-ID": "359341",
        "X-Instagram-AJAX": "1",
        "Referer": f"{BASE_URL}/accounts/onetap/",
    })

    # This endpoint is present in the authenticated HAR and only returns a
    # login_nonce for a usable logged-in session.
    try:
        response = session.post(
            f"{API_URL}/v1/web/accounts/request_one_tap_login_nonce/",
            headers=headers,
            data={},
            timeout=25,
            allow_redirects=False,
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "ok" and data.get("login_nonce"):
                return True
    except (requests.RequestException, ValueError):
        pass

    # Fallback: authenticated route bootstrap exposes a non-zero actorID.
    try:
        response = session.get(
            f"{BASE_URL}/accounts/onetap/?lsrc=ci",
            headers=authenticated_headers(session),
            timeout=25,
            allow_redirects=False,
        )
        if response.status_code == 200:
            actor_ids = re.findall(r'"actorID":"(\d+)"', response.text)
            if any(actor_id != "0" for actor_id in actor_ids):
                return True
    except requests.RequestException:
        pass
    return False


def login_with_cookies(session: requests.Session, cookie_text: str, save: bool = True) -> bool:
    cookies = parse_cookie_string(cookie_text)
    print(f"Cookie fields parsed: {', '.join(sorted(cookies)) or 'none'}")
    if not cookies.get("sessionid"):
        print("Cookie login failed: sessionid missing")
        return False

    # Apply first so the bootstrap request is authenticated. The visit also
    # supplies csrftoken when the pasted export omitted it.
    apply_cookies(session, cookies)
    try:
        session.get(BASE_URL, headers=DEFAULT_HEADERS, timeout=20)
    except requests.RequestException:
        pass
    try:
        valid = is_logged_in(session)
    except Exception:
        valid = False
    if not valid:
        print("Cookie login failed: session expired or invalid")
        return False

    if save:
        save_session(session, "cookie")
    print("Cookie login successful")
    return True


def get_current_user_info(session: requests.Session) -> dict:
    """Return the logged-in Instagram account's user ID and username."""
    user_id = str(session.cookies.get("ds_user_id", "") or "")
    username = ""

    try:
        response = session.get(
            f"{BASE_URL}/accounts/onetap/?lsrc=ci",
            headers=authenticated_headers(session),
            timeout=25,
            allow_redirects=False,
        )
        if response.status_code == 200:
            # Feed bootstrap has the most explicit current-viewer object.
            viewer = re.search(
                r'"xdt_viewer":\{"user":\{"username":"([^"]+)","id":"(\d+)"',
                response.text,
            )
            if viewer:
                username = viewer.group(1)
                user_id = viewer.group(2) or user_id
            else:
                # The one-tap route normally contains only the current username.
                names = list(dict.fromkeys(
                    re.findall(r'"username":"([A-Za-z0-9._]+)"', response.text)
                ))
                if len(names) == 1:
                    username = names[0]
    except requests.RequestException:
        pass

    return {
        "id": user_id or "unknown",
        "username": username or "unknown",
    }


def get_web_post_tokens(session: requests.Session):
    """Load dynamic web tokens used by Instagram's browser post flow."""
    headers = authenticated_headers(session)
    headers.update({"Accept": "text/html", "Referer": f"{BASE_URL}/"})
    try:
        response = session.get(f"{BASE_URL}/", headers=headers, timeout=30)
        if response.status_code != 200:
            print(f"Post token page failed: HTTP {response.status_code}")
            return None
    except requests.RequestException as exc:
        print(f"Post token error: {type(exc).__name__}")
        return None

    text = response.text or ""
    dtsg_match = re.search(
        r'DTSGInitialData"?,?\[\],\{"token":"([^"]+)"', text
    )
    rollout_match = re.search(r'"rollout_hash":"?([0-9]+)', text)
    if not dtsg_match:
        print("Post token missing: fb_dtsg")
        return None

    fb_dtsg = dtsg_match.group(1)
    # Standard Facebook web jazoest derived from the current DTSG token.
    jazoest = "2" + str(sum(ord(char) for char in fb_dtsg))
    claim = (
        response.headers.get("x-ig-set-www-claim")
        or response.headers.get("x-ig-www-claim")
        or "0"
    )
    return {
        "fb_dtsg": fb_dtsg,
        "jazoest": jazoest,
        "rollout_hash": rollout_match.group(1) if rollout_match else "",
        "claim": claim,
        "web_session_id": secrets.token_urlsafe(15)[:20],
    }


def upload_photo(session: requests.Session, image_path: str, web_tokens=None):
    if not os.path.exists(image_path):
        print(f"File not found: {image_path}")
        return None

    upload_id = str(int(time.time() * 1000))
    width = height = 1080
    temp_path = None

    try:
        from PIL import Image
        image = Image.open(image_path).convert("RGB")
        max_size = 1920
        if max(image.size) > max_size:
            ratio = max_size / max(image.size)
            image = image.resize(
                (int(image.size[0] * ratio), int(image.size[1] * ratio)),
                Image.Resampling.LANCZOS,
            )
        width, height = image.size
        temp_path = f"/tmp/ig_{upload_id}.jpg"
        image.save(temp_path, "JPEG", quality=95)
        image_path = temp_path
    except Exception as exc:
        print(f"Image conversion skipped: {exc}")

    file_size = os.path.getsize(image_path)
    if file_size <= 0:
        return None
    web_tokens = web_tokens or get_web_post_tokens(session)
    if not web_tokens:
        return None

    entity_name = f"fb_uploader_{upload_id}"
    rupload_params = {
        "media_type": 1,
        "upload_id": upload_id,
        "upload_media_height": int(height),
        "upload_media_width": int(width),
    }
    headers = authenticated_headers(session)
    headers.pop("X-Requested-With", None)
    headers.pop("X-CSRFToken", None)
    headers.update({
        "Content-Type": "image/jpeg",
        "X-Entity-Type": "image/jpeg",
        "X-Entity-Name": entity_name,
        "X-Entity-Length": str(file_size),
        "X-Instagram-Rupload-Params": json.dumps(
            rupload_params, separators=(",", ":")
        ),
        "X-ASBD-ID": "359341",
        "X-IG-Max-Touch-Points": "0",
        "X-Web-Session-ID": web_tokens["web_session_id"],
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Offset": "0",
    })
    if web_tokens.get("rollout_hash"):
        headers["X-Instagram-AJAX"] = web_tokens["rollout_hash"]

    try:
        # Match the successful browser flow and stream from disk to limit RAM.
        with open(image_path, "rb") as image_file:
            response = session.post(
                f"{UPLOAD_URL}/rupload_igphoto/{entity_name}",
                headers=headers,
                data=image_file,
                timeout=90,
            )
        if response.status_code == 200:
            return upload_id, width, height
        print(f"Upload failed: HTTP {response.status_code} {response.text[:300]}")
        return None
    except requests.RequestException as exc:
        print(f"Upload error: {exc}")
        return None
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass


def create_post(session: requests.Session, upload_id: str, width: int, height: int, caption: str = "", web_tokens=None) -> bool:
    web_tokens = web_tokens or get_web_post_tokens(session)
    if not web_tokens:
        return False
    data = {
        "upload_id": upload_id,
        "caption": caption,
        "archive_only": "false",
        "clips_share_preview_to_feed": "1",
        "disable_comments": "0",
        "disable_oa_reuse": "false",
        "fb_dtsg": web_tokens["fb_dtsg"],
        "igtv_share_preview_to_feed": "1",
        "is_meta_only_post": "0",
        "is_unified_video": "1",
        "jazoest": web_tokens["jazoest"],
        "like_and_view_counts_disabled": "0",
        "media_share_flow": "creation_flow",
        "share_to_facebook": "",
        "share_to_fb_destination_type": "USER",
        "source_type": "library",
        "video_subtitles_enabled": "0",
    }
    headers = authenticated_headers(session)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "X-ASBD-ID": "359341",
        "X-IG-Max-Touch-Points": "0",
        "X-IG-WWW-Claim": web_tokens.get("claim") or "0",
        "X-Web-Session-ID": web_tokens["web_session_id"],
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    })
    if web_tokens.get("rollout_hash"):
        headers["X-Instagram-AJAX"] = web_tokens["rollout_hash"]

    try:
        response = session.post(
            f"{API_URL}/v1/media/configure/",
            headers=headers,
            data=data,
            timeout=45,
        )
        if response.status_code == 200:
            print("Instagram post created")
            try:
                result = response.json()
            except ValueError:
                result = {}
            media = result.get("media") or {}
            code = media.get("code") or media.get("shortcode")
            return f"{BASE_URL}/p/{code}/" if code else True
        print(f"Configure failed: HTTP {response.status_code} {response.text[:300]}")
        return False
    except requests.RequestException as exc:
        print(f"Configure error: {exc}")
        return False


def post_photo(session: requests.Session, image_path: str, caption: str = "") -> bool:
    web_tokens = get_web_post_tokens(session)
    if not web_tokens:
        return False
    upload = upload_photo(session, image_path, web_tokens)
    if not upload:
        return False
    upload_id, width, height = upload
    return create_post(
        session, upload_id, width, height, caption, web_tokens=web_tokens
    )


def upload_video(
    session: requests.Session,
    video_path: str,
    width: int,
    height: int,
    duration: float,
    web_tokens=None,
):
    """Initialize and stream a clips video using Instagram's web flow."""
    if not os.path.exists(video_path):
        print("Video file not found")
        return None

    file_size = os.path.getsize(video_path)
    if file_size <= 0:
        return None

    web_tokens = web_tokens or get_web_post_tokens(session)
    if not web_tokens:
        return None

    width = max(1, int(width or 1080))
    height = max(1, int(height or 1350))
    duration = max(0.1, float(duration or 1))
    upload_id = str(int(time.time() * 1000))
    entity_name = f"fb_uploader_{upload_id}"
    crop_size = min(width, height)
    rupload_params = {
        "client-passthrough": "1",
        "for_album": False,
        "is_clips_video": "1",
        "is_sidecar": "0",
        "media_type": 2,
        "upload_id": upload_id,
        "upload_media_duration_ms": max(1, int(duration * 1000)),
        "upload_media_height": height,
        "upload_media_width": width,
        "video_edit_params": {
            "crop_height": crop_size,
            "crop_width": crop_size,
            "crop_x1": max(0, (width - crop_size) // 2),
            "crop_y1": max(0, (height - crop_size) // 2),
            "mute": False,
            "trim_end": duration,
            "trim_start": 0,
        },
        "video_format": "",
        "video_transform": None,
    }
    headers = authenticated_headers(session)
    headers.pop("X-Requested-With", None)
    headers.pop("X-CSRFToken", None)
    headers.update({
        "X-ASBD-ID": "359341",
        "X-IG-Max-Touch-Points": "0",
        "X-Web-Session-ID": web_tokens["web_session_id"],
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    })
    if web_tokens.get("rollout_hash"):
        headers["X-Instagram-AJAX"] = web_tokens["rollout_hash"]

    entity_url = f"{UPLOAD_URL}/rupload_igvideo/{entity_name}"
    try:
        init_response = session.get(entity_url, headers=headers, timeout=30)
        if init_response.status_code != 200:
            print(f"Video upload init failed: HTTP {init_response.status_code}")
            return None
    except requests.RequestException as exc:
        print(f"Video upload init error: {type(exc).__name__}")
        return None

    headers.update({
        "X-Entity-Name": entity_name,
        "X-Entity-Length": str(file_size),
        "X-Instagram-Rupload-Params": json.dumps(
            rupload_params, separators=(",", ":")
        ),
        "Offset": "0",
    })
    try:
        with open(video_path, "rb") as video_file:
            response = session.post(
                entity_url,
                headers=headers,
                data=video_file,
                timeout=180,
            )
        if response.status_code == 200:
            return upload_id
        print(f"Video upload failed: HTTP {response.status_code}")
    except requests.RequestException as exc:
        print(f"Video upload error: {type(exc).__name__}")
    return None


def upload_video_cover(
    session: requests.Session,
    cover_path: str,
    upload_id: str,
    width: int,
    height: int,
    web_tokens,
) -> bool:
    """Upload a JPEG cover under the same clips upload ID."""
    if not cover_path or not os.path.exists(cover_path):
        print("Video cover file missing")
        return False

    width = max(1, int(width or 1080))
    height = max(1, int(height or 1350))
    temp_path = f"/tmp/ig_video_cover_{upload_id}.jpg"
    try:
        from PIL import Image, ImageOps

        scale = min(1.0, 1920.0 / max(width, height))
        target_size = (
            max(1, int(width * scale)),
            max(1, int(height * scale)),
        )
        with Image.open(cover_path) as image:
            image = ImageOps.fit(
                image.convert("RGB"),
                target_size,
                method=Image.Resampling.LANCZOS,
            )
            image.save(temp_path, "JPEG", quality=92)
    except Exception as exc:
        print(f"Video cover conversion failed: {type(exc).__name__}")
        return False

    file_size = os.path.getsize(temp_path)
    entity_name = f"fb_uploader_{upload_id}"
    rupload_params = {
        "media_type": 2,
        "upload_id": upload_id,
        "upload_media_height": height,
        "upload_media_width": width,
    }
    headers = authenticated_headers(session)
    headers.pop("X-Requested-With", None)
    headers.pop("X-CSRFToken", None)
    headers.update({
        "Content-Type": "image/jpeg",
        "X-Entity-Type": "image/jpeg",
        "X-Entity-Name": entity_name,
        "X-Entity-Length": str(file_size),
        "X-Instagram-Rupload-Params": json.dumps(
            rupload_params, separators=(",", ":")
        ),
        "X-ASBD-ID": "359341",
        "X-IG-Max-Touch-Points": "0",
        "X-Web-Session-ID": web_tokens["web_session_id"],
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "Offset": "0",
    })
    if web_tokens.get("rollout_hash"):
        headers["X-Instagram-AJAX"] = web_tokens["rollout_hash"]

    try:
        with open(temp_path, "rb") as cover_file:
            response = session.post(
                f"{UPLOAD_URL}/rupload_igphoto/{entity_name}",
                headers=headers,
                data=cover_file,
                timeout=90,
            )
        if response.status_code == 200:
            return True
        print(f"Video cover upload failed: HTTP {response.status_code}")
    except requests.RequestException as exc:
        print(f"Video cover upload error: {type(exc).__name__}")
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return False


def _media_caption_text(media) -> str:
    if not isinstance(media, dict):
        return ""
    caption = media.get("caption")
    if isinstance(caption, dict):
        return str(caption.get("text") or "").strip()
    return str(media.get("caption_text") or caption or "").strip()


def _load_media_caption(session: requests.Session, media_id: str):
    try:
        response = session.get(
            f"{API_URL}/v1/media/{media_id}/info/",
            headers=authenticated_headers(session),
            timeout=30,
        )
        if response.status_code != 200:
            return None
        result = response.json()
        items = result.get("items") or []
        if not items:
            return None
        return _media_caption_text(items[0])
    except (requests.RequestException, ValueError):
        return None


def ensure_video_caption(
    session: requests.Session,
    media: dict,
    expected_caption: str,
    web_tokens=None,
) -> bool:
    """Verify the published caption and repair it once if Instagram dropped it."""
    expected_caption = str(expected_caption or "").strip()
    if not expected_caption:
        return False
    if _media_caption_text(media) == expected_caption:
        return True
    media_id = str(media.get("pk") or media.get("id") or "").split("_", 1)[0]
    if not media_id:
        print("Caption verification failed: media id unavailable")
        return False
    for attempt in range(3):
        actual = _load_media_caption(session, media_id)
        if actual == expected_caption:
            return True
        if attempt < 2:
            time.sleep(2)
    web_tokens = web_tokens or get_web_post_tokens(session)
    if not web_tokens:
        return False
    headers = authenticated_headers(session)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
        "X-ASBD-ID": "359341",
        "X-IG-WWW-Claim": web_tokens.get("claim") or "0",
    })
    if web_tokens.get("rollout_hash"):
        headers["X-Instagram-AJAX"] = web_tokens["rollout_hash"]
    try:
        response = session.post(
            f"{API_URL}/v1/web/media/{media_id}/edit/",
            headers=headers,
            data={"caption_text": expected_caption},
            timeout=45,
        )
        if response.status_code != 200:
            print(f"Caption repair failed: HTTP {response.status_code}")
            return False
        result = response.json()
        if result.get("status") != "ok" and not result.get("media"):
            return False
        repaired_media = result.get("media") or {}
        if _media_caption_text(repaired_media) == expected_caption:
            return True
    except (requests.RequestException, ValueError) as exc:
        print(f"Caption repair error: {type(exc).__name__}")
        return False
    for attempt in range(3):
        actual = _load_media_caption(session, media_id)
        if actual == expected_caption:
            return True
        if attempt < 2:
            time.sleep(2)
    print("Caption verification failed after repair")
    return False


def create_video_post(
    session: requests.Session,
    upload_id: str,
    width: int,
    height: int,
    duration: float,
    caption: str = "",
    web_tokens=None,
) -> bool:
    """Configure an uploaded video as an Instagram clip/reel."""
    web_tokens = web_tokens or get_web_post_tokens(session)
    if not web_tokens:
        return False
    data = {
        "upload_id": upload_id,
        "caption": caption,
        "archive_only": "false",
        "clips_share_preview_to_feed": "1",
        "disable_comments": "0",
        "disable_oa_reuse": "false",
        "fb_dtsg": web_tokens["fb_dtsg"],
        "igtv_share_preview_to_feed": "1",
        "is_meta_only_post": "0",
        "is_unified_video": "1",
        "jazoest": web_tokens["jazoest"],
        "like_and_view_counts_disabled": "0",
        "media_share_flow": "creation_flow",
        "share_to_facebook": "",
        "share_to_fb_destination_type": "USER",
        "source_type": "library",
        "video_subtitles_enabled": "0",
    }
    headers = authenticated_headers(session)
    headers.update({
        "Content-Type": "application/x-www-form-urlencoded",
        "X-ASBD-ID": "359341",
        "X-IG-Max-Touch-Points": "0",
        "X-IG-WWW-Claim": web_tokens.get("claim") or "0",
        "X-Web-Session-ID": web_tokens["web_session_id"],
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    })
    if web_tokens.get("rollout_hash"):
        headers["X-Instagram-AJAX"] = web_tokens["rollout_hash"]

    max_attempts = 12
    for attempt in range(max_attempts):
        if attempt:
            time.sleep(5)
        try:
            response = session.post(
                f"{API_URL}/v1/media/configure_to_clips/",
                headers=headers,
                data=data,
                timeout=90,
            )
            if response.status_code == 200:
                try:
                    result = response.json()
                except ValueError:
                    result = {}
                if result.get("status") == "ok" or result.get("media"):
                    media = result.get("media") or {}
                    caption_verified = ensure_video_caption(
                        session, media, caption, web_tokens
                    )
                    code = media.get("code") or media.get("shortcode")
                    post_url = f"{BASE_URL}/reel/{code}/" if code else ""
                    if caption_verified:
                        print("Instagram video post and caption verified")
                        return {
                            "video_created": True,
                            "caption_verified": True,
                            "post_url": post_url,
                            "reason": "",
                        }
                    # Preserve the created Reel URL but never report an
                    # unverified caption as an ordinary successful post.
                    print("Instagram video created; caption verification/repair failed")
                    return {
                        "video_created": True,
                        "caption_verified": False,
                        "post_url": post_url,
                        "reason": "Video created but caption verification/repair failed",
                    }
            print(
                f"Video configure pending/failed: HTTP {response.status_code} "
                f"attempt {attempt + 1}/{max_attempts}"
            )
        except requests.RequestException as exc:
            print(f"Video configure error: {type(exc).__name__}")
    return False


def post_video(
    session: requests.Session,
    video_path: str,
    caption: str = "",
    cover_path: str = "",
    width: int = 1080,
    height: int = 1350,
    duration: float = 1,
) -> bool:
    web_tokens = get_web_post_tokens(session)
    if not web_tokens:
        return False
    upload_id = upload_video(
        session,
        video_path,
        width,
        height,
        duration,
        web_tokens=web_tokens,
    )
    if not upload_id:
        return False
    if not upload_video_cover(
        session,
        cover_path,
        upload_id,
        width,
        height,
        web_tokens,
    ):
        return False
    return create_video_post(
        session,
        upload_id,
        width,
        height,
        duration,
        caption,
        web_tokens=web_tokens,
    )


def get_profile_edit_form_data(session: requests.Session):
    """Load editable profile fields so unrelated values can be preserved."""
    try:
        response = session.get(
            f"{API_URL}/v1/accounts/edit/web_form_data/",
            headers=authenticated_headers(session),
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            form = data.get("form_data") or data.get("user") or data
            return form if isinstance(form, dict) else None
        print(f"Profile form failed: HTTP {response.status_code}")
    except (requests.RequestException, ValueError) as exc:
        print(f"Profile form error: {type(exc).__name__}")
    return None


def update_profile_text_and_link(session: requests.Session, biography: str, external_url: str) -> bool:
    """Update only biography/link while preserving other editable profile fields."""
    biography = str(biography or "")
    external_url = str(external_url or "")
    if not biography or len(biography) > 150 or not external_url.startswith("https://"):
        return False
    form = get_profile_edit_form_data(session)
    if not form:
        return False
    current = get_current_user_info(session)
    payload = {
        "first_name": str(form.get("first_name") or form.get("full_name") or ""),
        "email": str(form.get("email") or ""),
        "username": str(form.get("username") or current.get("username") or ""),
        "phone_number": str(form.get("phone_number") or ""),
        "biography": biography,
        "external_url": external_url,
        "chaining_enabled": "on" if form.get("chaining_enabled", True) else "",
    }
    try:
        response = session.post(
            f"{API_URL}/v1/web/accounts/edit/",
            headers=authenticated_headers(session),
            data=payload,
            timeout=45,
        )
        if response.status_code == 200:
            result = response.json()
            return result.get("status") == "ok" or bool(result.get("user"))
        print(f"Profile text/link update failed: HTTP {response.status_code}")
    except (requests.RequestException, ValueError) as exc:
        print(f"Profile text/link error: {type(exc).__name__}")
    return False


def update_profile_picture(session: requests.Session, image_path: str) -> bool:
    """Center-crop a Telegram image and upload it as the profile picture."""
    if not image_path or not os.path.exists(image_path):
        return False
    temp_path = f"/tmp/ig_auto_profile_{int(time.time() * 1000)}.jpg"
    try:
        from PIL import Image, ImageOps
        with Image.open(image_path) as image:
            prepared = ImageOps.fit(
                image.convert("RGB"), (1080, 1080), method=Image.Resampling.LANCZOS
            )
            prepared.save(temp_path, "JPEG", quality=92)
        with open(temp_path, "rb") as handle:
            response = session.post(
                f"{API_URL}/v1/web/accounts/web_change_profile_picture/",
                headers=authenticated_headers(session),
                files={"profile_pic": ("profile.jpg", handle, "image/jpeg")},
                timeout=90,
            )
        if response.status_code == 200:
            result = response.json()
            return result.get("status") == "ok" or bool(result.get("profile_pic_url"))
        print(f"Profile picture update failed: HTTP {response.status_code}")
    except (OSError, requests.RequestException, ValueError) as exc:
        print(f"Profile picture error: {type(exc).__name__}")
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    return False


def get_profile(session: requests.Session, username: str):
    try:
        response = session.get(
            f"{API_URL}/v1/web/get_profile_pic_props/{username}/",
            headers=authenticated_headers(session),
            timeout=25,
        )
        if response.status_code == 200:
            return response.json()
        print(f"Profile failed: HTTP {response.status_code}")
    except (requests.RequestException, ValueError) as exc:
        print(f"Profile error: {exc}")
    return None


def main():
    parser = argparse.ArgumentParser(description="Instagram cookie-session helper")
    parser.add_argument("--cookie", help="Full Cookie header or raw sessionid")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--post", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--caption", default="")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    if args.cookie:
        sys.exit(0 if login_with_cookies(session, args.cookie) else 1)
    if not load_session(session):
        print("No saved cookie session")
        sys.exit(1)
    if args.check:
        print("VALID" if is_logged_in(session) else "EXPIRED")
        return
    if args.post:
        if not args.image:
            parser.error("--image is required with --post")
        sys.exit(0 if post_photo(session, args.image, args.caption) else 1)
    parser.print_help()


if __name__ == "__main__":
    main()
