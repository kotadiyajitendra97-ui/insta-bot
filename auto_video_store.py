  import os
import logging
from supabase import create_client, Client

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

supabase: Client = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase client initialized successfully for auto-videos.")
    except Exception as e:
        logger.error(f"Failed to initialize Supabase client: {e}")

def verify_auto_video_storage() -> bool:
    """
    Verifies if the 'auto_videos' table and storage bucket are accessible.
    Returns True if storage is available, False otherwise.
    """
    if not supabase:
        logger.error("❌ Supabase credentials not found in environment variables.")
        return False
    
    try:
        # Check table connectivity
        response = supabase.table("auto_videos").select("id", count="exact").limit(1).execute()
        logger.info("Auto-video storage table verified successfully.")
        return True
    except Exception as e:
        logger.error(f"❌ Auto-video storage unavailable. auto_video_setup.sql migration verify karo. Error: {e}")
        return False

def save_video_record(user_id: int, video_url: str, caption: str = ""):
    """
    Saves a video automation task record to Supabase.
    """
    if not supabase:
        logger.error("Supabase client not active.")
        return None
        
    try:
        data = {
            "user_id": user_id,
            "video_url": video_url,
            "caption": caption,
            "status": "pending"
        }
        response = supabase.table("auto_videos").insert(data).execute()
        return response
    except Exception as data_error:
        logger.error(f"Error saving video record: {data_error}")
        return None     
   
