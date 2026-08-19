#!/usr/bin/env python3
"""Owner-only Telegram bot using Instagram cookie login only."""

import gc
import hashlib
import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from auto_profile_store import (
    AutoProfileStoreError,
    get_auto_profile_settings,
    update_auto_profile_settings,
)
from auto_video_store import (
    AutoVideoStoreError,
    add_auto_video_links,
    auto_video_configured,
    clear_auto_video_links,
    get_auto_video_settings,
    list_auto_video_links,
    remove_auto_video_links,
    update_auto_video_settings,
)
from insta_auto import (
    DEFAULT_HEADERS,
    SESSION_FILE,
    get_current_user_info,
    get_profile,
    is_logged_in,
    update_profile_picture,
    update_profile_text_and_link,
    load_session,
    login_with_cookies,
    post_photo,
    post_video,
    save_session,
)
from supabase_store import (
    SupabaseStoreError,
    delete_account,
    delete_accounts,
    list_accounts,
    load_account,
    save_account,
    supabase_configured,
)

TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")


def parse_admin_ids(*values):
    """Parse comma, semicolon, space or newline separated Telegram user IDs."""
    parsed = []
    for value in values:
        normalized = str(value or "").replace(",", " ").replace(";", " ")
        for item in normalized.split():
            if item.isdigit() and item not in parsed:
                parsed.append(item)
    return tuple(parsed)


# The first ID remains the primary admin for the optional legacy local session.
# Saved accounts are scoped separately by each admin's own Telegram user ID.
TG_ADMIN_IDS = parse_admin_ids(
    os.environ.get("TG_ADMIN_ID", ""),
    os.environ.get("TG_ADMIN_IDS", ""),
)
TG_ADMIN_ID_SET = set(TG_ADMIN_IDS)
TG_ADMIN = TG_ADMIN_IDS[0] if TG_ADMIN_IDS else ""
IG_COOKIES = os.environ.get("IG_COOKIES", "")
TG_API = "https://api.telegram.org/bot" + TG_TOKEN
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
try:
    TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0") or 0)
except ValueError:
    TELEGRAM_API_ID = 0
try:
    MAX_VIDEO_SIZE_MB = max(
        1, min(100, int(os.environ.get("MAX_VIDEO_SIZE_MB", "100") or 100))
    )
except ValueError:
    MAX_VIDEO_SIZE_MB = 100
MAX_VIDEO_SIZE_BYTES = MAX_VIDEO_SIZE_MB * 1024 * 1024
BOT_API_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MAX_AUTO_VIDEO_LINKS = 50
VIDEO_CACHE_DIR = "/tmp/instaauto_video_cache"
VIDEO_CACHE_MAX_BYTES = 500 * 1024 * 1024
MAX_PARALLEL_UPLOADS = 10
try:
    MAX_SAVED_ACCOUNTS = int(os.environ["MAX_SAVED_ACCOUNTS"])
    if MAX_SAVED_ACCOUNTS < 1:
        raise ValueError
except (KeyError, ValueError):
    raise RuntimeError(
        "MAX_SAVED_ACCOUNTS must be set to a positive integer"
    )

_ig_sessions = {}
_chat_state = {}
_chat_admin_ids = {}
_mtproto_client = None


def is_admin(user_id):
    return str(user_id) in TG_ADMIN_ID_SET


def remember_chat_admin(chat_id, user_id):
    """Associate a chat with the authorized Telegram user controlling it."""
    _chat_admin_ids[str(chat_id)] = str(user_id)


def storage_owner_id(chat_id):
    """Use the actual admin Telegram ID as the isolated storage owner."""
    return _chat_admin_ids.get(str(chat_id), str(chat_id))


def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        return requests.post(
            f"{TG_API}/sendMessage", json=payload, timeout=30
        ).json()
    except Exception as exc:
        print(f"Telegram send error: {type(exc).__name__}")
        return None


def tg_answer_callback(callback_id, text=""):
    try:
        requests.post(
            f"{TG_API}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=10,
        )
    except Exception:
        pass


def tg_delete_message(chat_id, message_id):
    try:
        requests.post(
            f"{TG_API}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=10,
        )
    except Exception:
        pass


def mtproto_configured():
    return bool(TELEGRAM_API_ID and TELEGRAM_API_HASH and TG_TOKEN)


def get_mtproto_client():
    """Lazily connect as the bot over MTProto for files above 20 MB."""
    global _mtproto_client
    if not mtproto_configured():
        return None
    try:
        if _mtproto_client is None:
            from telethon.sync import TelegramClient

            _mtproto_client = TelegramClient(
                "/tmp/@Instaffgram_bot",
                TELEGRAM_API_ID,
                TELEGRAM_API_HASH,
            )
            _mtproto_client.start(bot_token=TG_TOKEN)
        elif not _mtproto_client.is_connected():
            _mtproto_client.connect()
        return _mtproto_client
    except Exception as exc:
        print(f"MTProto connect error: {type(exc).__name__}")
        _mtproto_client = None
        return None


def tg_download_large_file(
    file_id, chat_id=None, message_id=None, chat_username=None
):
    """Download through MTProto using file ID, then message fallback."""
    client = get_mtproto_client()
    if client is None or not file_id:
        return None
    local_path = f"/tmp/tg_large_{int(time.time() * 1000)}.mp4"
    try:
        from telethon.utils import resolve_bot_file_id

        media = resolve_bot_file_id(file_id)
        if media is not None:
            downloaded = client.download_file(media, file=local_path)
        else:
            print("MTProto file ID resolver unavailable; trying message fallback")
            if not message_id or not (chat_username or chat_id):
                return None
            peer = chat_username or int(chat_id)
            message = client.get_messages(peer, ids=int(message_id))
            if not message or not getattr(message, "media", None):
                print("MTProto message fallback returned no media")
                return None
            downloaded = client.download_media(message, file=local_path)
        if downloaded and os.path.exists(downloaded):
            return str(downloaded)
    except Exception as exc:
        print(f"MTProto download error: {type(exc).__name__}")
    try:
        os.remove(local_path)
    except OSError:
        pass
    return None


def tg_download_file(
    file_id,
    chat_id=None,
    message_id=None,
    file_size=0,
    chat_username=None,
):
    file_size = int(file_size or 0)
    if file_size > MAX_VIDEO_SIZE_BYTES:
        print("Telegram file rejected: above configured size limit")
        return None
    if file_size > BOT_API_DOWNLOAD_LIMIT:
        return tg_download_large_file(
            file_id,
            chat_id=chat_id,
            message_id=message_id,
            chat_username=chat_username,
        )

    try:
        result = requests.get(
            f"{TG_API}/getFile", params={"file_id": file_id}, timeout=30
        ).json()
        file_path = result.get("result", {}).get("file_path", "")
        if not file_path:
            return None
        download_url = (
            "https://api.telegram.org/file/bot" + TG_TOKEN + "/" + file_path
        )
        response = requests.get(download_url, timeout=120, stream=True)
        response.raise_for_status()
        suffix = os.path.splitext(file_path)[1].lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".m4v"}:
            suffix = ".bin"
        local_path = f"/tmp/tg_{int(time.time() * 1000)}{suffix}"
        with open(local_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        return local_path
    except Exception as exc:
        print(f"Telegram file download error: {type(exc).__name__}")
        return None


def menu_markup():
    return {
        "inline_keyboard": [
            [
                {"text": "🍪 Cookie Login", "callback_data": "cookie_login"},
                {"text": "💾 Saved Logins", "callback_data": "saved_logins"},
            ],
            [
                {"text": "📤 Post", "callback_data": "post_menu"},
            ],
            [
                {"text": "🎞️ Auto Videos", "callback_data": "auto_videos"},
                {"text": "✍️ Auto Caption", "callback_data": "auto_caption"},
            ],
            [
                {"text": "🖼️ Auto Thumbnail", "callback_data": "auto_thumbnail"},
                {"text": "🪪 Auto Profile", "callback_data": "auto_profile"},
            ],
            [
                {"text": "❌ Cancel", "callback_data": "cancel"},
            ],
        ]
    }


def cookie_help_text():
    return "🍪 Paste login cookie"


def new_instagram_session():
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    return session


def get_ig_session(chat_id):
    owner_id = storage_owner_id(chat_id)
    if owner_id not in _ig_sessions:
        session = new_instagram_session()
        # The old single local session belongs only to the primary admin.
        if owner_id == TG_ADMIN:
            load_session(session)
        _ig_sessions[owner_id] = session
    return _ig_sessions[owner_id]


def parse_public_telegram_video_link(value):
    text = str(value or "").strip().rstrip(".,;)")
    match = re.fullmatch(
        r"https?://(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/(\d+)(?:\?[^\s]*)?",
        text,
        flags=re.IGNORECASE,
    )
    if not match or match.group(1).lower() in {"c", "s", "joinchat"}:
        return None
    channel_username = match.group(1)
    message_id = int(match.group(2))
    if message_id < 1:
        return None
    return {
        "telegram_url": f"https://t.me/{channel_username}/{message_id}",
        "channel_username": channel_username,
        "message_id": message_id,
    }


def parse_selection_numbers(text, maximum):
    tokens = [item for item in re.split(r"[\s,]+", str(text or "").strip()) if item]
    if not tokens or any(not item.isdigit() for item in tokens):
        return None, "Sirf numbers bhejo. Example: 1,3"
    numbers = []
    for token in tokens:
        number = int(token)
        if number not in numbers:
            numbers.append(number)
    invalid = [number for number in numbers if number < 1 or number > maximum]
    if invalid:
        return None, f"Invalid number(s): {','.join(str(item) for item in invalid)}. Valid range: 1-{maximum}"
    return numbers, ""


def auto_video_storage_error(chat_id):
    tg_send(
        chat_id,
        "❌ Auto-video storage unavailable. auto_video_setup.sql migration verify karo.",
        reply_markup=menu_markup(),
    )


def show_auto_video_menu(chat_id):
    try:
        links = list_auto_video_links(storage_owner_id(chat_id))
    except AutoVideoStoreError as exc:
        print(f"Auto-video menu error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    tg_send(
        chat_id,
        "🎞️ <b>Auto Video Queue</b>\n\n"
        f"Saved links: {len(links)}/{MAX_AUTO_VIDEO_LINKS}",
        reply_markup={"inline_keyboard": [
            [{"text": "➕ Add Links", "callback_data": "auto_video_add"}],
            [{"text": "📋 View Links", "callback_data": "auto_video_view"}],
            [{"text": "➖ Remove Links", "callback_data": "auto_video_remove"}],
            [{"text": "🗑️ Clear All", "callback_data": "auto_video_clear"}],
            [{"text": "⬅️ Menu", "callback_data": "menu"}],
        ]},
    )


def start_auto_video_add_links(chat_id):
    _chat_state[str(chat_id)] = {"step": "auto_video_add_links"}
    tg_send(
        chat_id,
        "➕ <b>Add Public Telegram Video Links</b>\n\n"
        "Ek message mein multiple links bhejo, one per line.\n"
        "Example: <code>https://t.me/channelname/123</code>",
    )


def show_auto_video_links(chat_id):
    try:
        links = list_auto_video_links(storage_owner_id(chat_id))
    except AutoVideoStoreError as exc:
        print(f"Auto-video view error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    if not links:
        tg_send(chat_id, "🎞️ Abhi koi auto-video link saved nahi hai.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Auto Videos", "callback_data": "auto_videos"}]]})
        return
    lines = [f"{index}. <code>{html.escape(str(row.get('telegram_url') or ''))}</code>" for index, row in enumerate(links, start=1)]
    tg_send(chat_id, "📋 <b>Saved Auto-Video Links</b>\n\n" + "\n".join(lines), reply_markup={"inline_keyboard": [[{"text": "⬅️ Auto Videos", "callback_data": "auto_videos"}]]})


def start_auto_video_remove_links(chat_id):
    try:
        links = list_auto_video_links(storage_owner_id(chat_id))
    except AutoVideoStoreError as exc:
        print(f"Auto-video remove menu error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    if not links:
        tg_send(chat_id, "🎞️ Remove karne ke liye koi link nahi hai.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Auto Videos", "callback_data": "auto_videos"}]]})
        return
    snapshot = [{"number": index, "id": str(row.get("id") or ""), "url": str(row.get("telegram_url") or "")} for index, row in enumerate(links, start=1)]
    _chat_state[str(chat_id)] = {"step": "auto_video_remove_links", "links": snapshot}
    lines = [f"{item['number']}. <code>{html.escape(item['url'])}</code>" for item in snapshot]
    tg_send(chat_id, "➖ <b>Remove Video Links</b>\n\n" + "\n".join(lines) + "\n\nRemove karne wale numbers bhejo. Example: <code>1,3</code>")


def confirm_clear_auto_video_links(chat_id):
    tg_send(chat_id, "⚠️ <b>Clear All Auto-Video Links?</b>\n\nThis cannot be undone.", reply_markup={"inline_keyboard": [[{"text": "🗑️ Confirm Clear All", "callback_data": "auto_video_clear_confirm"}], [{"text": "❌ Cancel", "callback_data": "auto_videos"}]]})


def clear_all_auto_video_links(chat_id):
    try:
        rows = clear_auto_video_links(storage_owner_id(chat_id))
    except AutoVideoStoreError as exc:
        print(f"Auto-video clear error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    _chat_state.pop(str(chat_id), None)
    tg_send(chat_id, f"✅ Auto-video links cleared: {len(rows)}", reply_markup={"inline_keyboard": [[{"text": "⬅️ Auto Videos", "callback_data": "auto_videos"}]]})


def show_auto_caption_menu(chat_id):
    try:
        settings = get_auto_video_settings(storage_owner_id(chat_id))
    except AutoVideoStoreError as exc:
        print(f"Auto-caption menu error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    caption = str(settings.get("caption") or "")
    preview = html.escape(caption[:1200]) if caption else "Not configured"
    tg_send(chat_id, "✍️ <b>Auto Caption</b>\n\n" + preview, reply_markup={"inline_keyboard": [[{"text": "✏️ Set/Replace Caption", "callback_data": "auto_caption_set"}], [{"text": "🗑️ Clear Caption", "callback_data": "auto_caption_clear"}], [{"text": "⬅️ Menu", "callback_data": "menu"}]]})


def start_auto_caption(chat_id):
    _chat_state[str(chat_id)] = {"step": "auto_video_caption"}
    tg_send(chat_id, "✍️ Sab auto videos ke liye caption bhejo. Maximum 2200 characters.")


def clear_auto_caption(chat_id):
    try:
        update_auto_video_settings(storage_owner_id(chat_id), caption="")
    except AutoVideoStoreError as exc:
        print(f"Auto-caption clear error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    _chat_state.pop(str(chat_id), None)
    tg_send(chat_id, "✅ Auto caption cleared.", reply_markup=menu_markup())


def show_auto_thumbnail_menu(chat_id):
    try:
        settings = get_auto_video_settings(storage_owner_id(chat_id))
    except AutoVideoStoreError as exc:
        print(f"Auto-thumbnail menu error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    status = "Configured ✅" if settings.get("thumbnail_file_id") else "Not configured ❌"
    tg_send(chat_id, f"🖼️ <b>Auto Thumbnail</b>\n\nStatus: {status}", reply_markup={"inline_keyboard": [[{"text": "🖼️ Set/Replace Thumbnail", "callback_data": "auto_thumbnail_set"}], [{"text": "🗑️ Clear Thumbnail", "callback_data": "auto_thumbnail_clear"}], [{"text": "⬅️ Menu", "callback_data": "menu"}]]})


def start_auto_thumbnail(chat_id):
    _chat_state[str(chat_id)] = {"step": "auto_video_thumbnail"}
    tg_send(chat_id, "🖼️ Sab auto videos ke liye ek thumbnail <b>photo</b> ke roop mein bhejo.")


def ensure_video_cache_dir():
    os.makedirs(VIDEO_CACHE_DIR, exist_ok=True)
    return VIDEO_CACHE_DIR


def cache_key(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def video_cache_paths(telegram_url):
    key = cache_key(telegram_url)
    root = ensure_video_cache_dir()
    return (
        os.path.join(root, f"{key}.mp4"),
        os.path.join(root, f"{key}.json"),
        os.path.join(root, f"{key}.part.mp4"),
    )


def remove_cache_paths(*paths):
    for path in paths:
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def write_cache_metadata(path, metadata):
    temp_path = f"{path}.part"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            remove_cache_paths(temp_path)


def evict_video_cache(required_bytes=0, protected_path="", protected_paths=None):
    root = ensure_video_cache_dir()
    protected = set(protected_paths or [])
    if protected_path:
        protected.add(protected_path)
    entries = []
    total = 0
    for name in os.listdir(root):
        if not name.endswith(".mp4") or name.endswith(".part.mp4"):
            continue
        path = os.path.join(root, name)
        try:
            size = os.path.getsize(path)
            modified = os.path.getmtime(path)
        except OSError:
            continue
        total += size
        if path not in protected:
            entries.append((modified, path, size))
    for _modified, path, size in sorted(entries):
        if total + max(0, int(required_bytes or 0)) <= VIDEO_CACHE_MAX_BYTES:
            break
        metadata_path = os.path.splitext(path)[0] + ".json"
        remove_cache_paths(path, metadata_path)
        total -= size
    return total + max(0, int(required_bytes or 0)) <= VIDEO_CACHE_MAX_BYTES


def load_cached_video(telegram_url):
    video_path, metadata_path, partial_path = video_cache_paths(telegram_url)
    remove_cache_paths(partial_path, f"{metadata_path}.part")
    if not os.path.exists(video_path) or not os.path.exists(metadata_path):
        remove_cache_paths(video_path, metadata_path)
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as handle:
            metadata = json.load(handle)
        actual_size = os.path.getsize(video_path)
        expected_size = int(metadata.get("file_size") or 0)
        if (
            metadata.get("telegram_url") != telegram_url
            or actual_size < 1
            or actual_size != expected_size
            or actual_size > MAX_VIDEO_SIZE_BYTES
        ):
            raise ValueError("invalid cache metadata")
        now = time.time()
        os.utime(video_path, (now, now))
        os.utime(metadata_path, (now, now))
        return {
            "path": video_path,
            "width": max(1, int(metadata.get("width") or 1080)),
            "height": max(1, int(metadata.get("height") or 1350)),
            "duration": max(0.1, float(metadata.get("duration") or 1)),
            "cache_hit": True,
            "cache_managed": True,
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        remove_cache_paths(video_path, metadata_path)
        return None


def thumbnail_cache_path(file_id):
    return os.path.join(ensure_video_cache_dir(), f"thumb_{cache_key(file_id)}.img")


def invalidate_cached_thumbnail(file_id):
    if file_id:
        remove_cache_paths(thumbnail_cache_path(file_id))


def get_cached_thumbnail(file_id):
    path = thumbnail_cache_path(file_id)
    try:
        if os.path.getsize(path) > 0:
            now = time.time()
            os.utime(path, (now, now))
            return path, True
    except OSError:
        remove_cache_paths(path)
    downloaded = tg_download_file(file_id)
    if not downloaded:
        return None, False
    try:
        if os.path.getsize(downloaded) < 1:
            raise OSError("empty thumbnail")
        os.replace(downloaded, path)
        return path, False
    except OSError:
        remove_cache_paths(downloaded, path)
        return None, False


def cleanup_cache_partials():
    root = ensure_video_cache_dir()
    for name in os.listdir(root):
        if ".part" in name:
            remove_cache_paths(os.path.join(root, name))
    evict_video_cache()


def clear_auto_thumbnail(chat_id):
    try:
        settings = get_auto_video_settings(storage_owner_id(chat_id))
        old_file_id = str(settings.get("thumbnail_file_id") or "")
        update_auto_video_settings(storage_owner_id(chat_id), thumbnail_file_id="")
        invalidate_cached_thumbnail(old_file_id)
    except AutoVideoStoreError as exc:
        print(f"Auto-thumbnail clear error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    _chat_state.pop(str(chat_id), None)
    tg_send(chat_id, "✅ Auto thumbnail cleared.", reply_markup=menu_markup())


def download_public_telegram_video(
    channel_username, message_id, telegram_url="", protected_paths=None
):
    telegram_url = str(telegram_url or ("https://" + "t.me/" + str(channel_username) + "/" + str(int(message_id))))
    cached = load_cached_video(telegram_url)
    if cached:
        return cached, ""
    client = get_mtproto_client()
    if client is None:
        return None, "Telegram MTProto unavailable"
    video_path, metadata_path, partial_path = video_cache_paths(telegram_url)
    remove_cache_paths(partial_path)
    downloaded = ""
    try:
        message = client.get_messages(channel_username, ids=int(message_id))
        if not message:
            return None, "Telegram message not found"
        document = getattr(message, "document", None)
        mime_type = str(getattr(document, "mime_type", "") or "")
        if document is None or not mime_type.startswith("video/"):
            return None, "Telegram message has no video"
        file_size = int(getattr(document, "size", 0) or 0)
        if file_size > MAX_VIDEO_SIZE_BYTES:
            return None, f"Video exceeds {MAX_VIDEO_SIZE_MB} MB limit"
        if file_size > VIDEO_CACHE_MAX_BYTES:
            return None, "Video exceeds 500 MB deployment cache"
        if not evict_video_cache(
            file_size, protected_path=video_path, protected_paths=protected_paths
        ):
            return None, "Video cache has insufficient space"
        width, height, duration = 1080, 1350, 1.0
        for attribute in getattr(document, "attributes", []) or []:
            if attribute.__class__.__name__ == "DocumentAttributeVideo":
                width = int(getattr(attribute, "w", width) or width)
                height = int(getattr(attribute, "h", height) or height)
                duration = float(getattr(attribute, "duration", duration) or duration)
                break
        downloaded = str(client.download_media(message, file=partial_path) or "")
        if not downloaded or not os.path.exists(downloaded):
            return None, "Telegram video download failed"
        actual_size = os.path.getsize(downloaded)
        if actual_size < 1 or actual_size > MAX_VIDEO_SIZE_BYTES:
            return None, "Downloaded video size is invalid"
        if not evict_video_cache(
            actual_size, protected_path=video_path, protected_paths=protected_paths
        ):
            return None, "Video cache has insufficient space"
        os.replace(downloaded, video_path)
        downloaded = ""
        metadata = {
            "telegram_url": telegram_url,
            "file_size": actual_size,
            "width": max(1, width),
            "height": max(1, height),
            "duration": max(0.1, duration),
            "cached_at": int(time.time()),
        }
        write_cache_metadata(metadata_path, metadata)
        return {
            "path": video_path,
            "width": metadata["width"],
            "height": metadata["height"],
            "duration": metadata["duration"],
            "cache_hit": False,
            "cache_managed": True,
        }, ""
    except Exception as exc:
        print(f"Public Telegram video error: {type(exc).__name__}")
        return None, "Telegram channel/message inaccessible"
    finally:
        remove_cache_paths(partial_path)
        if downloaded and downloaded != video_path:
            remove_cache_paths(downloaded)
        if not os.path.exists(metadata_path):
            remove_cache_paths(video_path)


def video_post_result(result):
    """Normalize structured and legacy video-post return values."""
    if isinstance(result, dict):
        return {
            "video_created": bool(result.get("video_created")),
            "caption_verified": bool(result.get("caption_verified")),
            "post_url": str(result.get("post_url") or ""),
            "reason": str(result.get("reason") or ""),
        }
    return {
        "video_created": bool(result),
        "caption_verified": bool(result),
        "post_url": result if isinstance(result, str) else "",
        "reason": "" if result else "Instagram upload failed",
    }


def upload_auto_video_worker(
    cookie_values, media, caption, cover_path, worker_index
):
    caption = str(caption or "").strip()
    if not caption:
        print("Parallel auto-video rejected: caption snapshot missing")
        return False
    # Small stagger avoids millisecond upload-ID collisions while all workers
    # continue uploading concurrently. Each worker owns its HTTP session.
    # Fast mode: all ten workers start together with a tiny collision stagger.
    if worker_index:
        time.sleep(worker_index * 0.15)
    worker_session = new_instagram_session()
    worker_session.cookies.update(dict(cookie_values))
    try:
        return post_video(
            worker_session,
            media["path"],
            caption,
            cover_path=cover_path,
            width=media["width"],
            height=media["height"],
            duration=media["duration"],
        )
    except Exception as exc:
        print(f"Parallel auto-video error: {type(exc).__name__}")
        return False
    finally:
        worker_session.close()


def run_auto_video_queue(chat_id, session, account):
    owner_id = storage_owner_id(chat_id)
    try:
        settings = get_auto_video_settings(owner_id)
        links = list_auto_video_links(owner_id)
    except AutoVideoStoreError as exc:
        print(f"Auto-video queue load error: {type(exc).__name__}")
        auto_video_storage_error(chat_id)
        return
    if not links:
        tg_send(chat_id, "ℹ️ Auto-video queue empty hai; koi video post nahi hua.", reply_markup=menu_markup())
        return
    caption = str(settings.get("caption") or "").strip()
    thumbnail_file_id = str(settings.get("thumbnail_file_id") or "")
    if not caption:
        tg_send(chat_id, "⚠️ Auto-video queue stopped: Auto Caption configure karo.", reply_markup=menu_markup())
        return
    if not thumbnail_file_id:
        tg_send(chat_id, "⚠️ Auto-video queue stopped: Auto Thumbnail configure karo.", reply_markup=menu_markup())
        return
    if not mtproto_configured():
        tg_send(chat_id, "⚠️ Auto-video queue stopped: TELEGRAM_API_ID/HASH unavailable.", reply_markup=menu_markup())
        return
    cover_path, thumbnail_cache_hit = get_cached_thumbnail(thumbnail_file_id)
    if not cover_path:
        tg_send(chat_id, "⚠️ Auto-video queue stopped: saved thumbnail download failed.", reply_markup=menu_markup())
        return

    try:
        cookie_values = session.cookies.get_dict()
    except AttributeError:
        cookie_values = {cookie.name: cookie.value for cookie in session.cookies}
    if not cookie_values:
        tg_send(chat_id, "⚠️ Auto-video queue stopped: verified cookies unavailable.", reply_markup=menu_markup())
        return

    username = html.escape(str(account.get("username") or "unknown").lstrip("@"))
    tg_send(
        chat_id,
        f"🎞️ <b>Adaptive parallel queue started</b>\n\n"
        f"Account: @{username}\nVideos: {len(links)}\n"
        f"Maximum parallel uploads: {MAX_PARALLEL_UPLOADS}\nCache limit: 500 MB",
    )
    successes = []
    failures = []
    cache_hits = 0
    telegram_downloads = 0
    cursor = 0
    batch_number = 0

    while cursor < len(links):
        batch = []
        protected_paths = set()
        deferred_for_space = False
        tg_send(chat_id, f"⏳ <b>Preparing batch {batch_number + 1}</b>...")

        while cursor < len(links) and len(batch) < MAX_PARALLEL_UPLOADS:
            link = links[cursor]
            telegram_url = str(link.get("telegram_url") or "")
            media, reason = download_public_telegram_video(
                str(link.get("channel_username") or ""),
                int(link.get("message_id") or 0),
                telegram_url=telegram_url,
                protected_paths=protected_paths,
            )
            if not media:
                if reason == "Video cache has insufficient space" and batch:
                    deferred_for_space = True
                    break
                failures.append((telegram_url, reason, batch_number + 1))
                cursor += 1
                continue
            path = str(media.get("path") or "")
            try:
                media_size = os.path.getsize(path)
            except OSError:
                failures.append((telegram_url, "Prepared cache file missing", batch_number + 1))
                cursor += 1
                continue
            if media_size < 1 or media_size > VIDEO_CACHE_MAX_BYTES:
                failures.append((telegram_url, "Prepared video exceeds safe cache batch", batch_number + 1))
                cursor += 1
                continue
            protected_paths.add(path)
            batch.append({"link": link, "url": telegram_url, "media": media, "caption": caption})
            if media.get("cache_hit"):
                cache_hits += 1
            else:
                telegram_downloads += 1
            cursor += 1

        if not batch:
            if deferred_for_space and cursor < len(links):
                telegram_url = str(links[cursor].get("telegram_url") or "")
                failures.append((telegram_url, "Video cannot fit safe 500 MB batch", batch_number + 1))
                cursor += 1
            continue

        batch_number += 1
        tg_send(
            chat_id,
            f"🚀 <b>Batch {batch_number} upload started</b>\n\n"
            f"Parallel videos: {len(batch)}",
        )
        batch_successes = 0
        batch_failures = 0
        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            future_map = {}
            for worker_index, item in enumerate(batch):
                future = executor.submit(
                    upload_auto_video_worker,
                    cookie_values,
                    item["media"],
                    item["caption"],
                    cover_path,
                    worker_index,
                )
                future_map[future] = item

            for future in as_completed(future_map):
                item = future_map[future]
                telegram_url = item["url"]
                try:
                    posted = future.result()
                except Exception as exc:
                    print(f"Parallel future error: {type(exc).__name__}")
                    posted = False
                result = video_post_result(posted)
                if result["video_created"] and result["caption_verified"]:
                    successes.append((telegram_url, result["post_url"], batch_number))
                    batch_successes += 1
                else:
                    failures.append((
                        telegram_url,
                        result["reason"] or "Instagram upload failed",
                        batch_number,
                    ))
                    batch_failures += 1

        tg_send(
            chat_id,
            f"✅ <b>Batch {batch_number} complete</b>\n\n"
            f"Posted: {batch_successes}\nFailed: {batch_failures}",
        )
        for item in batch:
            if not item["media"].get("cache_managed"):
                remove_cache_paths(item["media"].get("path"))
        protected_paths.clear()
        evict_video_cache()
        gc.collect()

    lines = [
        f"Account: @{username}",
        f"Batches completed: {batch_number}",
        f"✅ Posted: {len(successes)}",
        f"❌ Failed: {len(failures)}",
        f"⚡ Cache hits: {cache_hits}",
        f"⬇️ Telegram downloads: {telegram_downloads}",
        f"🖼️ Thumbnail cache: {'HIT' if thumbnail_cache_hit else 'MISS'}",
    ]
    if failures:
        lines.append("\n<b>Failed links:</b>")
        lines.extend(
            f"Batch {batch}: <code>{html.escape(url)}</code> — {html.escape(reason)}"
            for url, reason, batch in failures
        )
    tg_send(
        chat_id,
        "📋 <b>Parallel Auto Video Results</b>\n\n" + "\n".join(lines),
        reply_markup=menu_markup(),
    )


def auto_profile_storage_error(chat_id):
    tg_send(chat_id, "❌ Auto Profile storage unavailable. auto_profile_setup.sql migration verify karo.", reply_markup=menu_markup())


def show_auto_profile_menu(chat_id):
    try:
        settings = get_auto_profile_settings(storage_owner_id(chat_id))
    except AutoProfileStoreError as exc:
        print(f"Auto Profile menu error: {type(exc).__name__}")
        auto_profile_storage_error(chat_id)
        return
    bio = html.escape(str(settings.get("bio_text") or "Not configured"))
    link = html.escape(str(settings.get("bio_link") or "Not configured"))
    dp = "Configured ✅" if settings.get("dp_telegram_file_id") else "Not configured ❌"
    tg_send(chat_id, f"🪪 <b>Auto Profile</b>\n\n🖼️ DP: {dp}\n✍️ Bio: {bio}\n🔗 Link: {link}", reply_markup={"inline_keyboard":[[{"text":"🖼️ Set/Replace DP","callback_data":"auto_profile_dp"}],[{"text":"✍️ Set/Replace Bio","callback_data":"auto_profile_bio"}],[{"text":"🔗 Set/Replace Bio Link","callback_data":"auto_profile_link"}],[{"text":"🗑️ Clear Settings","callback_data":"auto_profile_clear"}],[{"text":"⬅️ Menu","callback_data":"menu"}]]})


def start_auto_profile_dp(chat_id):
    _chat_state[str(chat_id)]={"step":"auto_profile_dp"}
    tg_send(chat_id,"🖼️ Auto DP ke liye photo bhejo. Square center-crop automatically hoga.")


def start_auto_profile_bio(chat_id):
    _chat_state[str(chat_id)]={"step":"auto_profile_bio"}
    tg_send(chat_id,"✍️ Auto Bio bhejo. Maximum 150 characters.")


def start_auto_profile_link(chat_id):
    _chat_state[str(chat_id)]={"step":"auto_profile_link"}
    tg_send(chat_id,"🔗 Valid HTTPS Bio Link bhejo. Example: <code>https://example.com</code>")


def clear_auto_profile(chat_id):
    try:
        update_auto_profile_settings(storage_owner_id(chat_id),bio_text="",bio_link="",dp_telegram_file_id="")
    except AutoProfileStoreError as exc:
        print(f"Auto Profile clear error: {type(exc).__name__}")
        auto_profile_storage_error(chat_id); return
    _chat_state.pop(str(chat_id),None)
    tg_send(chat_id,"✅ Auto Profile settings cleared.",reply_markup=menu_markup())


def run_auto_profile(chat_id, session, account):
    try:
        settings=get_auto_profile_settings(storage_owner_id(chat_id))
    except AutoProfileStoreError as exc:
        print(f"Auto Profile load error: {type(exc).__name__}")
        auto_profile_storage_error(chat_id); return
    missing=[]
    if not settings.get("dp_telegram_file_id"): missing.append("Auto DP")
    if not str(settings.get("bio_text") or "").strip(): missing.append("Auto Bio")
    if not str(settings.get("bio_link") or "").strip(): missing.append("Auto Bio Link")
    if missing:
        tg_send(chat_id,"⚠️ <b>Auto Profile skipped</b>\n\nTeeno settings required hain.\nMissing: " + ", ".join(missing)); return
    dp_path=tg_download_file(str(settings["dp_telegram_file_id"]))
    dp_ok=False
    text_ok=False
    try:
        if dp_path:
            dp_ok=update_profile_picture(session,dp_path)
        text_ok=update_profile_text_and_link(session,str(settings["bio_text"]),str(settings["bio_link"]))
    finally:
        remove_cache_paths(dp_path)
    username=html.escape(str(account.get("username") or "unknown").lstrip("@"))
    result=(f"🪪 <b>Auto Profile Result</b>\n\nAccount: @{username}\n" f"🖼️ DP: {'Updated ✅' if dp_ok else 'Failed ❌'}\n" f"✍️ Bio + Link: {'Updated ✅' if text_ok else 'Failed ❌'}\n\nAuto Videos continuing...")
    tg_send(chat_id,result)


def show_menu(chat_id):
    _chat_state.pop(str(chat_id), None)
    tg_send(
        chat_id,
        "🤖 <b>Instagram Cookie Bot</b>\n\n"
        "Password, email aur OTP login hata diya gaya hai. Sirf cookie login रहेगा।\n"
        "Photo/video + caption ko selected ya sab saved accounts par post kar sakte ho.",
        reply_markup=menu_markup(),
    )


def start_cookie_login(chat_id):
    _chat_state[str(chat_id)] = {"step": "cookie"}
    tg_send(chat_id, cookie_help_text())


def perform_cookie_login(chat_id, cookie_text):
    owner_id = storage_owner_id(chat_id)
    tg_send(chat_id, "⏳ Cookie session verify ho rahi hai...")
    session = new_instagram_session()
    if login_with_cookies(session, cookie_text, save=False):
        account = get_current_user_info(session)
        saved_text = ""
        if supabase_configured():
            try:
                accounts = list_accounts(owner_id)
            except SupabaseStoreError as exc:
                print(f"Account limit check error: {type(exc).__name__}")
                session.close()
                tg_send(
                    chat_id,
                    "⚠️ <b>Account limit check failed</b>\n\nPlease try again shortly.",
                    reply_markup=menu_markup(),
                )
                return

            account_id = str(account.get("id") or "")
            username = str(account.get("username") or "").lower().lstrip("@")
            already_saved = any(
                (
                    account_id
                    and account_id != "unknown"
                    and str(item.get("instagram_user_id") or "") == account_id
                )
                or (
                    username
                    and username != "unknown"
                    and str(item.get("username") or "").lower().lstrip("@")
                    == username
                )
                for item in accounts
            )
            if len(accounts) >= MAX_SAVED_ACCOUNTS and not already_saved:
                session.close()
                tg_send(
                    chat_id,
                    "🚫 <b>Account limit reached</b>\n\n"
                    f"🖥️ Server capability: maximum {MAX_SAVED_ACCOUNTS} accounts.\n"
                    "⬆️ Please upgrade the server to add more accounts.",
                    reply_markup=menu_markup(),
                )
                return

            try:
                save_account(owner_id, account, {cookie.name: cookie.value for cookie in session.cookies})
                saved_text = "\n💾 Login save ho gaya."
            except SupabaseStoreError as exc:
                print(f"Storage save error: {type(exc).__name__}")
                saved_text = "\n⚠️ Login successful, lekin session save nahi hui."
        if owner_id == TG_ADMIN:
            save_session(session)
        previous = _ig_sessions.get(owner_id)
        if previous is not None and previous is not session:
            try:
                previous.close()
            except Exception:
                pass
        _ig_sessions[owner_id] = session
        tg_send(
            chat_id,
            "✅ <b>Cookie login successful!</b>\n\n"
            f"User ID: <code>{account['id']}</code>\n"
            f"Username: <code>@{account['username']}</code>"
            f"{saved_text}",
            reply_markup=menu_markup(),
        )
        run_auto_profile(chat_id, session, account)
        run_auto_video_queue(chat_id, session, account)
    else:
        tg_send(
            chat_id,
            "❌ Cookie invalid/expired hai. Fresh Instagram cookies copy karke retry karo.",
            reply_markup=menu_markup(),
        )


def show_saved_logins(chat_id):
    if not supabase_configured():
        tg_send(
            chat_id,
            "⚠️ Saved logins storage configured nahi hai.",
            reply_markup=menu_markup(),
        )
        return

    try:
        accounts = list_accounts(storage_owner_id(chat_id))
    except SupabaseStoreError as exc:
        print(f"Storage list error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved logins load nahi hue.", reply_markup=menu_markup())
        return

    if not accounts:
        tg_send(
            chat_id,
            "💾 Abhi koi saved login nahi hai.",
            reply_markup=menu_markup(),
        )
        return

    current_id = str(get_ig_session(chat_id).cookies.get("ds_user_id", ""))
    keyboard = []
    names = []
    for number, account in enumerate(accounts[:MAX_SAVED_ACCOUNTS], start=1):
        username = str(account.get("username") or "unknown").lstrip("@")
        account_id = str(account.get("instagram_user_id") or "")
        prefix = "✅" if account_id and account_id == current_id else "👤"
        names.append(f"{number}. {prefix} @{username}")
        keyboard.append([{
            "text": f"{number}. {prefix} @{username}",
            "callback_data": f"saved_account:{account.get('id', '')}",
        }])
    keyboard.append([{"text": "🗑️ Select Accounts", "callback_data": "saved_logout_select"}])
    keyboard.append([{"text": f"🚪 Logout All ({len(accounts)})", "callback_data": "saved_logout_all"}])
    keyboard.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    tg_send(
        chat_id,
        "💾 <b>Saved Logins</b>\n\n" + "\n".join(names) +
        "\n\nUsername tap karo, ya bulk logout choose karo.",
        reply_markup={"inline_keyboard": keyboard},
    )


def show_saved_logout_selector(chat_id, selected_ids=None):
    """Start number-based selection; selected_ids is accepted for old callbacks."""
    owner_id = storage_owner_id(chat_id)
    try:
        accounts = list_accounts(owner_id)
    except SupabaseStoreError as exc:
        print(f"Numbered logout list error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved logins load nahi hue.", reply_markup=menu_markup())
        return
    if not accounts:
        _chat_state.pop(str(chat_id), None)
        tg_send(chat_id, "💾 Abhi koi saved login nahi hai.", reply_markup=menu_markup())
        return

    numbered_accounts = []
    lines = []
    for number, account in enumerate(accounts, start=1):
        username = str(account.get("username") or "unknown").lstrip("@")
        numbered_accounts.append({
            "number": number,
            "id": str(account.get("id") or ""),
            "username": username,
        })
        lines.append(f"{number}. @{html.escape(username)}")

    _chat_state[str(chat_id)] = {
        "step": "saved_logout_numbers",
        "numbered_accounts": numbered_accounts,
    }
    tg_send(
        chat_id,
        "🗑️ <b>Select Saved Logins</b>\n\n" + "\n".join(lines) +
        "\n\nPermanently logout karne wale account numbers bhejo.\n"
        "Example: <code>1,2,4,8</code>",
        reply_markup={"inline_keyboard": [[
            {"text": "⬅️ Saved Logins", "callback_data": "saved_logins"}
        ]]},
    )


def toggle_saved_logout_selection(chat_id, record_id):
    """Safely redirect buttons left in Telegram history to the new flow."""
    show_saved_logout_selector(chat_id)


def select_all_saved_logouts(chat_id):
    """Safely redirect the retired checkbox Select All button."""
    show_saved_logout_selector(chat_id)


def handle_saved_logout_numbers(chat_id, text):
    state = _chat_state.get(str(chat_id), {})
    snapshot = state.get("numbered_accounts", [])
    if state.get("step") != "saved_logout_numbers" or not snapshot:
        tg_send(chat_id, "⚠️ Number selection expire ho gayi. Saved Logins se retry karo.", reply_markup=menu_markup())
        return True

    raw_tokens = [token for token in re.split(r"[\s,]+", str(text or "").strip()) if token]
    if not raw_tokens or any(not token.isdigit() for token in raw_tokens):
        tg_send(chat_id, "⚠️ Sirf account numbers bhejo. Example: <code>1,2,4,8</code>")
        return True

    numbers = []
    for token in raw_tokens:
        number = int(token)
        if number not in numbers:
            numbers.append(number)
    by_number = {item["number"]: item for item in snapshot}
    invalid = [number for number in numbers if number not in by_number]
    if invalid:
        tg_send(
            chat_id,
            "⚠️ Invalid account number(s): <code>" +
            html.escape(",".join(str(number) for number in invalid)) +
            f"</code>\nValid range: <code>1-{len(snapshot)}</code>",
        )
        return True

    selected = [by_number[number] for number in numbers]
    selected_ids = [item["id"] for item in selected]
    _chat_state[str(chat_id)] = {
        "step": "saved_logout_confirm",
        "mode": "selected",
        "selected_ids": selected_ids,
    }
    lines = [f"{item['number']}. @{html.escape(item['username'])}" for item in selected]
    tg_send(
        chat_id,
        "⚠️ <b>Logout Selected Accounts</b>\n\n" + "\n".join(lines) +
        f"\n\n{len(selected)} saved login(s) permanently delete hongi.\n"
        "This cannot be undone.",
        reply_markup={"inline_keyboard": [
            [{
                "text": f"🗑️ Confirm Permanent Logout ({len(selected)})",
                "callback_data": "saved_logout_bulk_confirm:selected",
            }],
            [{"text": "❌ Cancel", "callback_data": "saved_logout_select"}],
        ]},
    )
    return True


def confirm_bulk_saved_logout(chat_id, mode):
    try:
        accounts = list_accounts(storage_owner_id(chat_id))
    except SupabaseStoreError as exc:
        print(f"Bulk confirm error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved logins load nahi hue.", reply_markup=menu_markup())
        return
    account_by_id = {str(a.get("id") or ""): a for a in accounts}
    if mode == "all":
        selected = set(account_by_id)
    else:
        state = _chat_state.get(str(chat_id), {})
        selected = set(state.get("selected_ids", [])) & set(account_by_id) if state.get("step") == "saved_logout_select" else set()
    if not selected:
        tg_send(chat_id, "⚠️ Pehle kam se kam ek account select karo.", reply_markup={"inline_keyboard": [[{"text": "⬅️ Select Accounts", "callback_data": "saved_logout_select"}]]})
        return
    _chat_state[str(chat_id)] = {"step": "saved_logout_confirm", "mode": mode, "selected_ids": sorted(selected)}
    names = [f"• @{html.escape(str(account_by_id[x].get('username') or 'unknown').lstrip('@'))}" for x in sorted(selected)]
    title = "Logout All Saved Accounts" if mode == "all" else "Logout Selected Accounts"
    cancel = "saved_logins" if mode == "all" else "saved_logout_select"
    tg_send(chat_id, f"⚠️ <b>{title}</b>\n\n{len(selected)} saved login(s) permanently delete hongi:\n" + "\n".join(names) + "\n\nThis cannot be undone.", reply_markup={"inline_keyboard": [[{"text": f"🗑️ Confirm Permanent Logout ({len(selected)})", "callback_data": f"saved_logout_bulk_confirm:{mode}"}], [{"text": "❌ Cancel", "callback_data": cancel}]]})


def clear_bulk_deleted_active_session(owner_id, instagram_ids):
    deleted = {str(x) for x in instagram_ids if str(x)}
    session = _ig_sessions.get(owner_id)
    active_id = str(session.cookies.get("ds_user_id", "")) if session is not None else ""
    if not active_id and owner_id == TG_ADMIN and os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as f:
                active_id = str((json.load(f).get("cookies") or {}).get("ds_user_id", ""))
        except (OSError, ValueError, AttributeError):
            active_id = ""
    if active_id not in deleted:
        return
    if session is not None:
        try: session.close()
        except Exception: pass
    _ig_sessions.pop(owner_id, None)
    if owner_id == TG_ADMIN:
        try: os.remove(SESSION_FILE)
        except OSError: pass


def permanent_logout_saved_bulk(chat_id, mode):
    owner_id = storage_owner_id(chat_id)
    state = _chat_state.get(str(chat_id), {})
    if state.get("step") != "saved_logout_confirm" or state.get("mode") != mode:
        tg_send(chat_id, "⚠️ Bulk logout confirmation expire ho gayi.", reply_markup=menu_markup())
        return
    try:
        accounts = list_accounts(owner_id)
    except SupabaseStoreError as exc:
        print(f"Bulk refresh error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved logins verify nahi hue.", reply_markup=menu_markup())
        return
    by_id = {str(a.get("id") or ""): a for a in accounts}
    target = sorted(set(state.get("selected_ids", [])) & set(by_id))
    if not target:
        _chat_state.pop(str(chat_id), None)
        tg_send(chat_id, "⚠️ Selected accounts ab available nahi hain.", reply_markup=menu_markup())
        return
    try:
        rows = delete_accounts(owner_id, target)
    except SupabaseStoreError as exc:
        print(f"Bulk permanent logout error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Bulk logout verify nahi hua. Saved Logins refresh karke check karo.", reply_markup={"inline_keyboard": [[{"text": "💾 Saved Logins", "callback_data": "saved_logins"}]]})
        return
    deleted_ids = {str(r.get("id") or "") for r in rows}
    deleted_accounts = [by_id[x] for x in target if x in deleted_ids]
    clear_bulk_deleted_active_session(owner_id, [a.get("instagram_user_id", "") for a in deleted_accounts])
    _chat_state.pop(str(chat_id), None)
    deleted_count = len(deleted_accounts)
    missing = len(target) - deleted_count
    remaining = max(0, len(accounts) - deleted_count)
    text = (f"⚠️ <b>Bulk logout partially verified</b>\n\nDeleted: {deleted_count}\nNot verified: {missing}\nRemaining before refresh: {remaining}" if missing else f"✅ <b>Bulk logout complete</b>\n\nDeleted: {deleted_count}\nRemaining: {remaining}")
    tg_send(chat_id, text, reply_markup={"inline_keyboard": [[{"text": "💾 Saved Logins", "callback_data": "saved_logins"}], [{"text": "⬅️ Menu", "callback_data": "menu"}]]})


def show_saved_account_actions(chat_id, record_id):
    try:
        saved = load_account(storage_owner_id(chat_id), record_id)
    except SupabaseStoreError as exc:
        print(f"Storage account menu error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved account load nahi hua.", reply_markup=menu_markup())
        return

    username = html.escape(str(saved.get("username") or "unknown").lstrip("@"))
    user_id = html.escape(str(saved.get("instagram_user_id") or "unknown"))
    keyboard = {
        "inline_keyboard": [
            [{"text": "🔐 Login", "callback_data": f"saved_use:{record_id}"}],
            [{"text": "🍪 Export Cookie", "callback_data": f"saved_export:{record_id}"}],
            [{"text": "🚪 Logout", "callback_data": f"saved_logout:{record_id}"}],
            [{"text": "⬅️ Saved Logins", "callback_data": "saved_logins"}],
        ]
    }
    tg_send(
        chat_id,
        "👤 <b>Saved Account</b>\n\n"
        f"Username: <code>@{username}</code>\n"
        f"User ID: <code>{user_id}</code>\n\n"
        "Choose an option:",
        reply_markup=keyboard,
    )


def export_saved_cookie(chat_id, record_id):
    try:
        saved = load_account(storage_owner_id(chat_id), record_id)
    except SupabaseStoreError as exc:
        print(f"Storage cookie export error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Cookie export nahi hui.", reply_markup=menu_markup())
        return

    cookies = saved.get("cookies") or {}
    cookie_header = "; ".join(
        f"{name}={value}" for name, value in sorted(cookies.items())
    )
    if not cookie_header:
        tg_send(chat_id, "❌ Saved cookie empty hai.", reply_markup=menu_markup())
        return

    username = html.escape(str(saved.get("username") or "unknown").lstrip("@"))
    safe_cookie = html.escape(cookie_header)
    tg_send(
        chat_id,
        f"🍪 <b>Cookie Export — @{username}</b>\n\n"
        f"<code>{safe_cookie}</code>\n\n"
        "⚠️ Keep this cookie private. Anyone with it may access the account.",
        reply_markup={
            "inline_keyboard": [[
                {"text": "⬅️ Account Options", "callback_data": f"saved_account:{record_id}"}
            ]]
        },
    )


def confirm_saved_logout(chat_id, record_id):
    try:
        saved = load_account(storage_owner_id(chat_id), record_id)
    except SupabaseStoreError as exc:
        print(f"Storage logout menu error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved account load nahi hua.", reply_markup=menu_markup())
        return

    username = html.escape(str(saved.get("username") or "unknown").lstrip("@"))
    tg_send(
        chat_id,
        "⚠️ <b>Permanent Logout</b>\n\n"
        f"@{username} ki saved login permanently delete ho jayegi.\n"
        "This cannot be undone.",
        reply_markup={
            "inline_keyboard": [
                [{
                    "text": "🗑️ Confirm Permanent Logout",
                    "callback_data": f"saved_logout_confirm:{record_id}",
                }],
                [{
                    "text": "❌ Cancel",
                    "callback_data": f"saved_account:{record_id}",
                }],
            ]
        },
    )


def permanent_logout_saved(chat_id, record_id):
    owner_id = storage_owner_id(chat_id)
    try:
        saved = load_account(owner_id, record_id)
        delete_account(owner_id, record_id)
    except SupabaseStoreError as exc:
        print(f"Storage permanent logout error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Account permanently delete nahi hua.", reply_markup=menu_markup())
        return

    deleted_user_id = str(saved.get("instagram_user_id") or "")
    active_user_id = ""
    active_session = _ig_sessions.get(owner_id)
    if active_session is not None:
        active_user_id = str(active_session.cookies.get("ds_user_id", ""))
    elif owner_id == TG_ADMIN and os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, encoding="utf-8") as session_file:
                local_data = json.load(session_file)
            active_user_id = str(
                (local_data.get("cookies") or {}).get("ds_user_id", "")
            )
        except (OSError, ValueError, AttributeError):
            active_user_id = ""

    if deleted_user_id and deleted_user_id == active_user_id:
        if active_session is not None:
            try:
                active_session.close()
            except Exception:
                pass
        _ig_sessions.pop(owner_id, None)
        if owner_id == TG_ADMIN:
            try:
                os.remove(SESSION_FILE)
            except OSError:
                pass

    username = html.escape(str(saved.get("username") or "unknown").lstrip("@"))
    tg_send(
        chat_id,
        "✅ <b>Permanent logout complete</b>\n\n"
        f"@{username} permanently logout ho gaya aur saved login delete ho gayi.",
        reply_markup={
            "inline_keyboard": [
                [{"text": "💾 Saved Logins", "callback_data": "saved_logins"}],
                [{"text": "⬅️ Menu", "callback_data": "menu"}],
            ]
        },
    )


def activate_saved_login(chat_id, record_id):
    owner_id = storage_owner_id(chat_id)
    tg_send(chat_id, "⏳ Saved login verify ho raha hai...")
    try:
        saved = load_account(owner_id, record_id)
    except SupabaseStoreError as exc:
        print(f"Storage load error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Saved login load nahi hua.", reply_markup=menu_markup())
        return

    session = new_instagram_session()
    if not login_with_cookies(session, json.dumps(saved.get("cookies", {})), save=False):
        tg_send(
            chat_id,
            "❌ Is account ki Instagram session expire ho gayi. Fresh Cookie Login karo.",
            reply_markup=menu_markup(),
        )
        return

    previous = _ig_sessions.get(owner_id)
    if previous is not None and previous is not session:
        try:
            previous.close()
        except Exception:
            pass
    _ig_sessions[owner_id] = session
    if owner_id == TG_ADMIN:
        save_session(session)
    account = get_current_user_info(session)
    if account.get("username") == "unknown":
        account["username"] = saved.get("username", "unknown")
    if account.get("id") == "unknown":
        account["id"] = saved.get("instagram_user_id", "unknown")
    try:
        save_account(owner_id, account, {cookie.name: cookie.value for cookie in session.cookies})
    except SupabaseStoreError as exc:
        print(f"Storage refresh error: {type(exc).__name__}")
    tg_send(
        chat_id,
        "✅ <b>Account active ho gaya!</b>\n\n"
        f"User ID: <code>{account['id']}</code>\n"
        f"Username: <code>@{account['username']}</code>",
        reply_markup=menu_markup(),
    )


def handle_status(chat_id):
    session = get_ig_session(chat_id)
    if is_logged_in(session):
        account = get_current_user_info(session)
        tg_send(
            chat_id,
            "✅ <b>Cookie session valid hai.</b>\n\n"
            f"User ID: <code>{account['id']}</code>\n"
            f"Username: <code>@{account['username']}</code>",
            reply_markup=menu_markup(),
        )
    else:
        tg_send(
            chat_id,
            "❌ Session missing ya expired hai. Cookie Login use karo.",
            reply_markup=menu_markup(),
        )


def handle_logout(chat_id):
    owner_id = storage_owner_id(chat_id)
    _chat_state.pop(str(chat_id), None)
    session = _ig_sessions.pop(owner_id, None)
    if session is not None:
        try:
            session.close()
        except Exception:
            pass
    if owner_id == TG_ADMIN and os.path.exists(SESSION_FILE):
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass
    tg_send(chat_id, "✅ Cookie session delete ho gayi.", reply_markup=menu_markup())


def handle_profile(chat_id, username):
    session = get_ig_session(chat_id)
    if not is_logged_in(session):
        tg_send(chat_id, "❌ Pehle Cookie Login karo.", reply_markup=menu_markup())
        return
    tg_send(chat_id, f"⏳ Profile fetch ho rahi hai: <b>{username}</b>")
    profile = get_profile(session, username)
    if profile:
        preview = json.dumps(profile, indent=2)[:3000]
        tg_send(chat_id, f"📋 <code>{preview}</code>", reply_markup=menu_markup())
    else:
        tg_send(chat_id, "❌ Profile fetch nahi hui.", reply_markup=menu_markup())


def show_post_targets(chat_id):
    if not supabase_configured():
        tg_send(
            chat_id,
            "⚠️ Saved logins storage configured nahi hai.",
            reply_markup=menu_markup(),
        )
        return
    try:
        accounts = list_accounts(storage_owner_id(chat_id))
    except SupabaseStoreError as exc:
        print(f"Post account list error: {type(exc).__name__}")
        tg_send(chat_id, "❌ Accounts load nahi hue.", reply_markup=menu_markup())
        return
    if not accounts:
        tg_send(
            chat_id,
            "❌ Pehle Cookie Login se account save karo.",
            reply_markup=menu_markup(),
        )
        return

    keyboard = [[{
        "text": "🌐 All Accounts",
        "callback_data": "post_target:all",
    }]]
    for account in accounts[:MAX_SAVED_ACCOUNTS]:
        username = str(account.get("username") or "unknown").lstrip("@")
        keyboard.append([{
            "text": f"👤 @{username}",
            "callback_data": f"post_target:{account.get('id', '')}",
        }])
    keyboard.append([{"text": "⬅️ Menu", "callback_data": "menu"}])
    tg_send(
        chat_id,
        "📤 <b>Post kis account par karna hai?</b>",
        reply_markup={"inline_keyboard": keyboard},
    )


def select_post_target(chat_id, target):
    label = "🌐 All Accounts"
    if target != "all":
        try:
            saved = load_account(storage_owner_id(chat_id), target)
            label = f"👤 @{str(saved.get('username') or 'unknown').lstrip('@')}"
        except SupabaseStoreError as exc:
            print(f"Post target error: {type(exc).__name__}")
            tg_send(chat_id, "❌ Account select nahi hua.", reply_markup=menu_markup())
            return
    _chat_state[str(chat_id)] = {"step": "post_media", "target": target}
    tg_send(
        chat_id,
        f"✅ Target: <b>{label}</b>\n\n"
        "📤 Ab photo ya video <b>caption ke saath</b> bhejo.",
    )


def handle_post_media(chat_id, message):
    state = _chat_state.get(str(chat_id), {})
    if state.get("step") == "auto_profile_dp":
        photos=message.get("photo",[])
        if not photos:
            tg_send(chat_id,"❌ Auto DP ko photo ke roop mein bhejo."); return
        file_id=str(photos[-1].get("file_id") or "")
        try:
            update_auto_profile_settings(storage_owner_id(chat_id),dp_telegram_file_id=file_id)
        except AutoProfileStoreError as exc:
            print(f"Auto DP save error: {type(exc).__name__}"); auto_profile_storage_error(chat_id); return
        _chat_state.pop(str(chat_id),None)
        tg_send(chat_id,"✅ Auto DP saved. Bio aur Link bhi configure karo.",reply_markup=menu_markup()); return
    if state.get("step") == "auto_video_thumbnail":
        photos = message.get("photo", [])
        if not photos:
            tg_send(chat_id, "❌ Auto thumbnail ko photo ke roop mein bhejo.")
            return
        file_id = str(photos[-1].get("file_id") or "")
        if not file_id:
            tg_send(chat_id, "❌ Thumbnail file ID missing. Photo dobara bhejo.")
            return
        try:
            settings = get_auto_video_settings(storage_owner_id(chat_id))
            old_file_id = str(settings.get("thumbnail_file_id") or "")
            update_auto_video_settings(storage_owner_id(chat_id), thumbnail_file_id=file_id)
            if old_file_id and old_file_id != file_id:
                invalidate_cached_thumbnail(old_file_id)
        except AutoVideoStoreError as exc:
            print(f"Auto-thumbnail save error: {type(exc).__name__}")
            auto_video_storage_error(chat_id)
            return
        _chat_state.pop(str(chat_id), None)
        tg_send(chat_id, "✅ Auto thumbnail saved. Har queued video par ye thumbnail use hogi.", reply_markup=menu_markup())
        return
    if state.get("step") == "post_thumbnail":
        custom_photos = message.get("photo", [])
        if not custom_photos:
            tg_send(
                chat_id,
                "🖼️ Custom thumbnail ko <b>photo</b> ke roop mein bhejo, "
                "ya Auto Thumbnail choose karo.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "⚡ Auto Thumbnail", "callback_data": "video_thumbnail_auto"}],
                        [{"text": "❌ Cancel", "callback_data": "video_thumbnail_cancel"}],
                    ]
                },
            )
            return

        pending_message = state.get("pending_message")
        if not isinstance(pending_message, dict):
            show_menu(chat_id)
            return
        _chat_state[str(chat_id)] = {
            "step": "post_media",
            "target": state.get("target", ""),
            "thumbnail_ready": True,
            "custom_cover_file_id": custom_photos[-1].get("file_id", ""),
        }
        tg_send(chat_id, "✅ Custom thumbnail selected. Upload start ho raha hai...")
        handle_post_media(chat_id, pending_message)
        return

    if state.get("step") != "post_media":
        tg_send(
            chat_id,
            "📤 Pehle Post tab se All Accounts ya username select karo.",
            reply_markup=menu_markup(),
        )
        return

    photos = message.get("photo", [])
    video = message.get("video") or {}
    cover_file_id = ""
    video_file_size = 0
    if photos:
        media_type = "photo"
        file_id = photos[-1].get("file_id", "")
        width = height = 0
        duration = 0
    elif video:
        media_type = "video"
        file_id = video.get("file_id", "")
        thumbnail = video.get("thumbnail") or video.get("thumb") or {}
        cover_file_id = str(
            state.get("custom_cover_file_id")
            or thumbnail.get("file_id")
            or ""
        )
        video_file_size = int(video.get("file_size") or 0)
        width = int(video.get("width") or 1080)
        height = int(video.get("height") or 1350)
        duration = float(video.get("duration") or 1)
    else:
        return

    caption = str(message.get("caption") or "").strip()
    if not caption:
        tg_send(chat_id, "❌ Media ke saath caption bhi add karo.")
        return
    if len(caption) > 2200:
        tg_send(chat_id, "❌ Caption 2200 characters se chhota rakho.")
        return

    target = state.get("target", "")
    if media_type == "video" and video_file_size > MAX_VIDEO_SIZE_BYTES:
        tg_send(
            chat_id,
            f"❌ Video {MAX_VIDEO_SIZE_MB} MB se chhota hona chahiye.",
            reply_markup=menu_markup(),
        )
        return
    if (
        media_type == "video"
        and video_file_size > BOT_API_DOWNLOAD_LIMIT
        and not mtproto_configured()
    ):
        tg_send(
            chat_id,
            "❌ 20 MB se badi video ke liye TELEGRAM_API_ID aur TELEGRAM_API_HASH required hain.",
            reply_markup=menu_markup(),
        )
        return
    if media_type == "video" and not state.get("thumbnail_ready"):
        _chat_state[str(chat_id)] = {
            "step": "post_thumbnail",
            "target": target,
            "pending_message": message,
        }
        tg_send(
            chat_id,
            "🖼️ <b>Choose Video Thumbnail</b>\n\n"
            "Custom thumbnail ke liye ab ek photo bhejo.\n"
            "Recommended: JPG, 1080×1920, 9:16.\n\n"
            "Ya video ka automatic thumbnail use karo.",
            reply_markup={
                "inline_keyboard": [
                    [{"text": "⚡ Auto Thumbnail", "callback_data": "video_thumbnail_auto"}],
                    [{"text": "❌ Cancel", "callback_data": "video_thumbnail_cancel"}],
                ]
            },
        )
        return

    _chat_state.pop(str(chat_id), None)
    local_path = tg_download_file(
        file_id,
        chat_id=chat_id,
        message_id=message.get("message_id"),
        file_size=video_file_size,
        chat_username=(message.get("chat", {}) or {}).get("username"),
    )
    if not local_path:
        tg_send(chat_id, "❌ Telegram media download failed.", reply_markup=menu_markup())
        return

    cover_path = None
    if media_type == "video":
        if cover_file_id:
            cover_path = tg_download_file(cover_file_id)
        if not cover_path:
            try:
                os.remove(local_path)
            except OSError:
                pass
            tg_send(
                chat_id,
                "❌ Thumbnail download failed. Video ko dobara bhejkar custom photo ya Auto Thumbnail choose karo.",
                reply_markup=menu_markup(),
            )
            return

    try:
        if target == "all":
            try:
                summaries = list_accounts(storage_owner_id(chat_id))[:MAX_SAVED_ACCOUNTS]
            except SupabaseStoreError as exc:
                print(f"Post list error: {type(exc).__name__}")
                summaries = []
        else:
            summaries = [{"id": target}]

        if not summaries:
            tg_send(chat_id, "❌ Koi saved account nahi mila.", reply_markup=menu_markup())
            return

        tg_send(
            chat_id,
            f"⏳ {media_type.title()} {len(summaries)} account(s) par post ho raha hai...",
        )
        results = []
        total = len(summaries)
        for index, summary in enumerate(summaries, start=1):
            username = str(summary.get("username") or "unknown").lstrip("@")
            try:
                saved = load_account(storage_owner_id(chat_id), str(summary.get("id", "")))
                username = str(saved.get("username") or username).lstrip("@")
            except SupabaseStoreError as exc:
                print(f"Post account load error: {type(exc).__name__}")
                results.append(f"❌ @{username}: load failed")
                tg_send(chat_id, f"❌ [{index}/{total}] @{username}\nLoad failed")
                gc.collect()
                if index < total:
                    time.sleep(2)
                continue

            tg_send(
                chat_id,
                f"⏳ [{index}/{total}] @{username}\n{media_type.title()} post ho raha hai...",
            )
            session = new_instagram_session()
            if not login_with_cookies(
                session, json.dumps(saved.get("cookies", {})), save=False
            ):
                results.append(f"❌ @{username}: session expired")
                tg_send(chat_id, f"❌ [{index}/{total}] @{username}\nSession expired")
                session.close()
                del session
                gc.collect()
                if index < total:
                    time.sleep(2)
                continue

            posted = False
            try:
                if media_type == "photo":
                    posted = post_photo(session, local_path, caption)
                else:
                    posted = post_video(
                        session,
                        local_path,
                        caption,
                        cover_path=cover_path,
                        width=width,
                        height=height,
                        duration=duration,
                    )
            except Exception as exc:
                # Isolate each account: one unexpected failure must not stop
                # the remaining sequential queue.
                print(f"Account post error: {type(exc).__name__}")
                posted = False
            finally:
                # Release cookies, HTTP pools and upload objects before the
                # result message and before starting the next account.
                session.close()
                del session
                del saved
                gc.collect()

            if posted:
                post_url = posted if isinstance(posted, str) else ""
                results.append(f"✅ @{username}: done")
                done_text = f"✅ [{index}/{total}] @{username}\n<b>Done</b>"
                if post_url:
                    done_text += f"\n🔗 {post_url}"
                else:
                    done_text += "\n🔗 URL response mein nahi mila"
                tg_send(chat_id, done_text)
            else:
                results.append(f"❌ @{username}: post failed")
                tg_send(chat_id, f"❌ [{index}/{total}] @{username}\nPost failed")

            # Posting is strictly sequential; wait briefly before next login.
            if index < total:
                time.sleep(2)

        tg_send(
            chat_id,
            "📋 <b>Post Results</b>\n\n" + "\n".join(results),
            reply_markup=menu_markup(),
        )
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass
        if cover_path:
            try:
                os.remove(cover_path)
            except OSError:
                pass


def continue_video_with_auto_thumbnail(chat_id):
    state = _chat_state.get(str(chat_id), {})
    if state.get("step") != "post_thumbnail":
        tg_send(chat_id, "❌ Koi pending video nahi hai.", reply_markup=menu_markup())
        return
    pending_message = state.get("pending_message")
    if not isinstance(pending_message, dict):
        show_menu(chat_id)
        return
    _chat_state[str(chat_id)] = {
        "step": "post_media",
        "target": state.get("target", ""),
        "thumbnail_ready": True,
        "custom_cover_file_id": "",
    }
    tg_send(chat_id, "⚡ Auto thumbnail selected. Upload start ho raha hai...")
    handle_post_media(chat_id, pending_message)


def handle_callback(callback):
    user_id = callback.get("from", {}).get("id", 0)
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id", 0)
    callback_id = callback.get("id", "")
    data = callback.get("data", "")

    if not is_admin(user_id):
        tg_answer_callback(callback_id, "Unauthorized")
        if chat_id:
            tg_send(chat_id, "❌ Unauthorized. Sirf authorized admins use kar sakte hain.")
        return

    remember_chat_admin(chat_id, user_id)
    tg_answer_callback(callback_id)
    if data == "cookie_login":
        start_cookie_login(chat_id)
    elif data == "saved_logins":
        show_saved_logins(chat_id)
    elif data.startswith("saved_account:"):
        show_saved_account_actions(chat_id, data.split(":", 1)[1])
    elif data.startswith("saved_use:"):
        activate_saved_login(chat_id, data.split(":", 1)[1])
    elif data.startswith("saved_export:"):
        export_saved_cookie(chat_id, data.split(":", 1)[1])
    elif data == "saved_logout_select":
        show_saved_logout_selector(chat_id)
    elif data.startswith("saved_logout_toggle:"):
        toggle_saved_logout_selection(chat_id, data.split(":", 1)[1])
    elif data == "saved_logout_select_all":
        select_all_saved_logouts(chat_id)
    elif data == "saved_logout_clear":
        show_saved_logout_selector(chat_id, [])
    elif data == "saved_logout_selected":
        show_saved_logout_selector(chat_id)
    elif data == "saved_logout_all":
        confirm_bulk_saved_logout(chat_id, "all")
    elif data.startswith("saved_logout_bulk_confirm:"):
        permanent_logout_saved_bulk(chat_id, data.split(":", 1)[1])
    elif data.startswith("saved_logout_confirm:"):
        permanent_logout_saved(chat_id, data.split(":", 1)[1])
    elif data.startswith("saved_logout:"):
        confirm_saved_logout(chat_id, data.split(":", 1)[1])
    elif data == "auto_profile":
        show_auto_profile_menu(chat_id)
    elif data == "auto_profile_dp":
        start_auto_profile_dp(chat_id)
    elif data == "auto_profile_bio":
        start_auto_profile_bio(chat_id)
    elif data == "auto_profile_link":
        start_auto_profile_link(chat_id)
    elif data == "auto_profile_clear":
        clear_auto_profile(chat_id)
    elif data == "auto_videos":
        show_auto_video_menu(chat_id)
    elif data == "auto_video_add":
        start_auto_video_add_links(chat_id)
    elif data == "auto_video_view":
        show_auto_video_links(chat_id)
    elif data == "auto_video_remove":
        start_auto_video_remove_links(chat_id)
    elif data == "auto_video_clear":
        confirm_clear_auto_video_links(chat_id)
    elif data == "auto_video_clear_confirm":
        clear_all_auto_video_links(chat_id)
    elif data == "auto_caption":
        show_auto_caption_menu(chat_id)
    elif data == "auto_caption_set":
        start_auto_caption(chat_id)
    elif data == "auto_caption_clear":
        clear_auto_caption(chat_id)
    elif data == "auto_thumbnail":
        show_auto_thumbnail_menu(chat_id)
    elif data == "auto_thumbnail_set":
        start_auto_thumbnail(chat_id)
    elif data == "auto_thumbnail_clear":
        clear_auto_thumbnail(chat_id)
    elif data == "post_menu":
        show_post_targets(chat_id)
    elif data.startswith("post_target:"):
        select_post_target(chat_id, data.split(":", 1)[1])
    elif data == "video_thumbnail_auto":
        continue_video_with_auto_thumbnail(chat_id)
    elif data == "video_thumbnail_cancel":
        show_menu(chat_id)
    elif data == "status":
        handle_status(chat_id)
    elif data == "profile":
        _chat_state[str(chat_id)] = {"step": "profile"}
        tg_send(chat_id, "👤 Instagram username bhejo:")
    elif data == "logout":
        handle_logout(chat_id)
    else:
        show_menu(chat_id)


def handle_text_state(chat_id, message, text):
    state = _chat_state.get(str(chat_id))
    if not state:
        return False

    step = state.get("step")
    if step == "cookie":
        _chat_state.pop(str(chat_id), None)
        # Never retain the sensitive cookie message in Telegram chat.
        tg_delete_message(chat_id, message.get("message_id"))
        perform_cookie_login(chat_id, text.strip())
        return True
    if step == "profile":
        _chat_state.pop(str(chat_id), None)
        handle_profile(chat_id, text.strip())
        return True
    if step == "saved_logout_numbers":
        return handle_saved_logout_numbers(chat_id, text)
    if step == "auto_profile_bio":
        bio=str(text or "").strip()
        if not bio or len(bio)>150:
            tg_send(chat_id,"⚠️ Bio 1-150 characters ka hona chahiye."); return True
        try:
            update_auto_profile_settings(storage_owner_id(chat_id),bio_text=bio)
        except AutoProfileStoreError as exc:
            print(f"Auto Bio save error: {type(exc).__name__}"); auto_profile_storage_error(chat_id); return True
        _chat_state.pop(str(chat_id),None); tg_send(chat_id,"✅ Auto Bio saved.",reply_markup=menu_markup()); return True
    if step == "auto_profile_link":
        link=str(text or "").strip()
        if not re.fullmatch(r"https://[^\s]{3,2048}",link):
            tg_send(chat_id,"⚠️ Valid HTTPS link bhejo."); return True
        try:
            update_auto_profile_settings(storage_owner_id(chat_id),bio_link=link)
        except AutoProfileStoreError as exc:
            print(f"Auto Bio Link save error: {type(exc).__name__}"); auto_profile_storage_error(chat_id); return True
        _chat_state.pop(str(chat_id),None); tg_send(chat_id,"✅ Auto Bio Link saved.",reply_markup=menu_markup()); return True
    if step == "auto_profile_dp":
        tg_send(chat_id,"🖼️ Auto DP ke liye photo bhejo."); return True
    if step == "auto_video_add_links":
        candidates = [item for item in re.split(r"[\s]+", str(text or "").strip()) if item]
        parsed = []
        invalid = []
        for candidate in candidates:
            item = parse_public_telegram_video_link(candidate)
            if item:
                if item["telegram_url"] not in {row["telegram_url"] for row in parsed}:
                    parsed.append(item)
            else:
                invalid.append(candidate)
        if invalid or not parsed:
            tg_send(chat_id, "⚠️ Invalid public Telegram link(s). Format: <code>https://t.me/channelname/123</code>")
            return True
        try:
            existing = list_auto_video_links(storage_owner_id(chat_id))
            known_urls = {str(row.get("telegram_url") or "") for row in existing}
            new_count = sum(item["telegram_url"] not in known_urls for item in parsed)
            if len(existing) + new_count > MAX_AUTO_VIDEO_LINKS:
                tg_send(chat_id, f"⚠️ Maximum {MAX_AUTO_VIDEO_LINKS} auto-video links allowed.")
                return True
            added = add_auto_video_links(storage_owner_id(chat_id), parsed)
        except AutoVideoStoreError as exc:
            print(f"Auto-video add error: {type(exc).__name__}")
            auto_video_storage_error(chat_id)
            return True
        _chat_state.pop(str(chat_id), None)
        tg_send(chat_id, f"✅ New auto-video links added: {len(added)}", reply_markup={"inline_keyboard": [[{"text": "⬅️ Auto Videos", "callback_data": "auto_videos"}]]})
        return True
    if step == "auto_video_remove_links":
        snapshot = state.get("links", [])
        numbers, error = parse_selection_numbers(text, len(snapshot))
        if error:
            tg_send(chat_id, f"⚠️ {html.escape(error)}")
            return True
        by_number = {item["number"]: item for item in snapshot}
        record_ids = [by_number[number]["id"] for number in numbers]
        try:
            removed = remove_auto_video_links(storage_owner_id(chat_id), record_ids)
        except AutoVideoStoreError as exc:
            print(f"Auto-video remove error: {type(exc).__name__}")
            auto_video_storage_error(chat_id)
            return True
        _chat_state.pop(str(chat_id), None)
        tg_send(chat_id, f"✅ Auto-video links removed: {len(removed)}", reply_markup={"inline_keyboard": [[{"text": "⬅️ Auto Videos", "callback_data": "auto_videos"}]]})
        return True
    if step == "auto_video_caption":
        caption = str(text or "").strip()
        if not caption or len(caption) > 2200:
            tg_send(chat_id, "⚠️ Caption 1-2200 characters ka hona chahiye.")
            return True
        try:
            update_auto_video_settings(storage_owner_id(chat_id), caption=caption)
        except AutoVideoStoreError as exc:
            print(f"Auto-caption save error: {type(exc).__name__}")
            auto_video_storage_error(chat_id)
            return True
        _chat_state.pop(str(chat_id), None)
        tg_send(chat_id, "✅ Auto caption saved. Har queued video par ye caption use hoga.", reply_markup=menu_markup())
        return True
    if step == "auto_video_thumbnail":
        tg_send(chat_id, "🖼️ Auto thumbnail ke liye ek photo bhejo.")
        return True
    if step == "post_media":
        tg_send(chat_id, "📤 Photo ya video caption ke saath bhejo.")
        return True
    if step == "post_thumbnail":
        tg_send(chat_id, "🖼️ Custom thumbnail photo bhejo ya Auto Thumbnail choose karo.")
        return True
    return False


def process_message(message):
    user_id = message.get("from", {}).get("id", 0)
    chat_id = message.get("chat", {}).get("id", 0)
    text = message.get("text", "")

    if not is_admin(user_id):
        tg_send(chat_id, "❌ Unauthorized. Sirf authorized admins use kar sakte hain.")
        return

    remember_chat_admin(chat_id, user_id)
    if text == "/start" or text == "/help":
        show_menu(chat_id)
    elif text == "/login":
        start_cookie_login(chat_id)
    elif text == "/status":
        handle_status(chat_id)
    elif text == "/saved":
        show_saved_logins(chat_id)
    elif text == "/post":
        show_post_targets(chat_id)
    elif text == "/logout":
        handle_logout(chat_id)
    elif text == "/cancel":
        show_menu(chat_id)
    elif text.startswith("/profile "):
        handle_profile(chat_id, text.split(maxsplit=1)[1].strip())
    elif message.get("photo") or message.get("video"):
        handle_post_media(chat_id, message)
    elif text and handle_text_state(chat_id, message, text):
        return
    else:
        tg_send(chat_id, "Menu se option choose karo.", reply_markup=menu_markup())


def run_bot():
    print("\n🤖 Instagram Cookie Bot Starting...")
    if not TG_TOKEN:
        print("❌ TG_BOT_TOKEN missing")
        sys.exit(1)
    if not TG_ADMIN_IDS:
        print("❌ TG_ADMIN_ID or TG_ADMIN_IDS missing")
        sys.exit(1)
    print(f"   Admin IDs ({len(TG_ADMIN_IDS)}): {', '.join(TG_ADMIN_IDS)}")
    print("   Saved accounts: isolated per admin Telegram ID")
    print(f"   Saved logins: {'enabled' if supabase_configured() else 'disabled'}")
    print(f"   Auto videos: {'enabled' if auto_video_configured() else 'disabled'}")
    cleanup_cache_partials()
    print("   Video cache: 500 MB deployment-local")

    if IG_COOKIES:
        session = new_instagram_session()
        if login_with_cookies(session, IG_COOKIES, save=True):
            _ig_sessions[TG_ADMIN] = session
            print("   Cookie auto-login successful")
        else:
            print("   IG_COOKIES invalid/expired; use Telegram Cookie Login")
    else:
        session = new_instagram_session()
        if load_session(session) and is_logged_in(session):
            _ig_sessions[TG_ADMIN] = session
            print("   Saved cookie session valid")
        else:
            print("   No valid cookie session; use /login")

    try:
        bot = requests.get(f"{TG_API}/getMe", timeout=10).json().get("result", {})
        print(f"   Bot: @{bot.get('username', 'unknown')}")
    except Exception as exc:
        print(f"   Bot info error: {type(exc).__name__}")

    offset = 0
    print("   ✅ Bot running! Waiting for messages...\n")
    while True:
        try:
            result = requests.get(
                f"{TG_API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=35,
            ).json()
            if not result.get("ok"):
                print(f"Telegram API error: {result}")
                time.sleep(10 if result.get("error_code") == 409 else 5)
                continue
            for update in result.get("result", []):
                offset = update.get("update_id", 0) + 1
                if update.get("callback_query"):
                    handle_callback(update["callback_query"])
                elif update.get("message"):
                    process_message(update["message"])
        except requests.exceptions.Timeout:
            continue
        except Exception as exc:
            # Exception text can contain Cookie headers; never print it.
            print(f"Bot loop error: {type(exc).__name__}")
            time.sleep(5)


from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=10000)

if __name__ == "__main__":
    # Web server ko background thread mein start karein taaki Render ka port requirement pura ho jaye
    threading.Thread(target=run_web).start()
    # Phir apna Telegram bot run karein
    run_bot()
