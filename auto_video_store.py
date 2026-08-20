import os
from supabase import create_client, Client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")

def get_supabase() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def get_links(owner_id: str) -> list:
    try:
        supabase = get_supabase()
        response = supabase.table("instagram_video_links").select("*").eq("owner_id", owner_id).execute()
        return response.data if response.data else []
    except Exception as e:
        print(f"Error getting links: {e}")
        return []

def add_link(owner_id: str, video_link: str):
    try:
        supabase = get_supabase()
        supabase.table("instagram_video_links").insert({
            "owner_id": owner_id,
            "video_link": video_link
        }).execute()
        return True
    except Exception as e:
        print(f"Error adding link: {e}")
        return False

def delete_link(link_id: str):
    try:
        supabase = get_supabase()
        supabase.table("instagram_video_links").delete().eq("id", link_id).execute()
        return True
    except Exception as e:
        print(f"Error deleting link: {e}")
        return False

def clear_all_links(owner_id: str):
    try:
        supabase = get_supabase()
        supabase.table("instagram_video_links").delete().eq("owner_id", owner_id).execute()
        return True
    except Exception as e:
        print(f"Error clearing links: {e}")
        return False
