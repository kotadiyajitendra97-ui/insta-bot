import os
import time
import logging
import requests
from supabase_store import get_all_active_settings # ya jo aapka store function ho
from auto_video_store import get_links, delete_link

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

def get_all_users_settings():
    """Supabase se sabhi connected users ki settings fetch karta hai."""
    try:
        headers = {
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": "application/json"
        }
        res = requests.get(f"{SUPABASE_URL}/rest/v1/instagram_settings?select=*", headers=headers, timeout=30)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        logger.error(f"Error fetching settings: {e}")
    return []

def update_instagram_profile(cookie_string, bio_text, bio_link):
    """Instagram par Bio update karta hai."""
    headers = {
        "User-Agent": "Instagram 293.0.0.30.115 Android",
        "Cookie": cookie_string,
        "X-IG-App-ID": "936619743392459"
    }
    data = {
        "biography": f"{bio_text}\n{bio_link}" if bio_link else bio_text
    }
    try:
        res = requests.post("https://i.instagram.com/api/v1/accounts/edit_profile/", headers=headers, data=data, timeout=20)
        if res.status_code == 200:
            logger.info("Instagram Profile/Bio updated successfully via Worker!")
            return True
    except Exception as e:
        logger.error(f"Failed to update profile: {e}")
    return False

def background_worker():
    """Background loop jo har kuch der mein checks chalayega."""
    logger.info("Instagram Background Automation Worker Started...")
    while True:
        try:
            users = get_all_users_settings()
            for user in users:
                owner_id = user.get("owner_id")
                cookie = user.get("ig_cookie")
                bio = user.get("auto_caption") or user.get("bio_text", "")
                
                if not cookie:
                    continue
                
                # 1. Update Bio agar present hai
                if bio:
                    update_instagram_profile(cookie, bio, "")
                
                # 2. Check for Video Links to Post
                links = get_links(owner_id)
                if links:
                    target_link = links[0] # Pehla link uthao
                    logger.info(f"Found video link to process for user {owner_id}: {target_link.get('video_link')}")
                    # Yahan video download aur posting ka logic execute hoga
                    # Post hone ke baad link delete kar dein taaki dobara post na ho
                    delete_link(owner_id, target_link.get("id"))
                
        except Exception as e:
            logger.error(f"Worker loop error: {e}")
            
        # Har 5 minute mein check karega
        time.sleep(300)

if __name__ == "__main__":
    background_worker()
