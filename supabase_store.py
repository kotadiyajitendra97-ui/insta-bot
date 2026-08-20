import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")

def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Supabase URL or Key missing in environment variables.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_settings(owner_id: str) -> dict:
    try:
        supabase = get_supabase()
        response = supabase.table("instagram_settings").select("*").eq("owner_id", owner_id).execute()
        if response.data and len(response.data) > 0:
            return response.data[0]
        return {}
    except Exception as e:
        print(f"Error getting settings: {e}")
        return {}

def save_settings(owner_id: str, **kwargs):
    try:
        supabase = get_supabase()
        existing = get_settings(owner_id)
        
        data = {"owner_id": owner_id}
        data.update(kwargs)
        
        if existing:
            supabase.table("instagram_settings").update(kwargs).eq("owner_id", owner_id).execute()
        else:
            supabase.table("instagram_settings").insert(data).execute()
        return True
    except Exception as e:
        print(f"Error saving settings: {e}")
        return False

def clear_account_settings(owner_id: str):
    try:
        supabase = get_supabase()
        supabase.table("instagram_settings").delete().eq("owner_id", owner_id).execute()
        return True
    except Exception as e:
        print(f"Error clearing settings: {e}")
        return False
