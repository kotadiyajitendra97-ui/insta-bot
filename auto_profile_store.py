#!/usr/bin/env python3
"""Owner-scoped persistent Instagram Auto Profile settings."""
import os
from datetime import datetime, timezone
import requests

SUPABASE_URL=os.environ.get("SUPABASE_URL","").rstrip("/")
SUPABASE_SERVICE_KEY=os.environ.get("SUPABASE_SERVICE_KEY","")
TABLE="instagram_auto_profile_settings"

class AutoProfileStoreError(RuntimeError):
    pass

def _configured():
    return bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def _headers(prefer=None):
    if not _configured():
        raise AutoProfileStoreError("Supabase environment variables missing")
    result={"apikey":SUPABASE_SERVICE_KEY,"Content-Type":"application/json"}
    if SUPABASE_SERVICE_KEY.count(".")==2:
        result["Authorization"]=f"Bearer {SUPABASE_SERVICE_KEY}"
    if prefer:
        result["Prefer"]=prefer
    return result

def _url():
    return f"{SUPABASE_URL}/rest/v1/{TABLE}"

def get_auto_profile_settings(owner_id):
    try:
        response=requests.get(_url(),params={"select":"owner_id,bio_text,bio_link,dp_telegram_file_id,updated_at","owner_id":f"eq.{owner_id}","limit":"1"},headers=_headers(),timeout=30)
        response.raise_for_status(); rows=response.json()
    except (requests.RequestException,ValueError) as exc:
        raise AutoProfileStoreError("Could not load Auto Profile settings") from exc
    if not isinstance(rows,list):
        raise AutoProfileStoreError("Invalid Auto Profile response")
    if not rows:
        return {"owner_id":str(owner_id),"bio_text":"","bio_link":"","dp_telegram_file_id":""}
    row=rows[0]
    return {"owner_id":str(owner_id),"bio_text":str(row.get("bio_text") or ""),"bio_link":str(row.get("bio_link") or ""),"dp_telegram_file_id":str(row.get("dp_telegram_file_id") or ""),"updated_at":row.get("updated_at")}

def update_auto_profile_settings(owner_id,**updates):
    allowed={"bio_text","bio_link","dp_telegram_file_id"}
    if set(updates)-allowed:
        raise AutoProfileStoreError("Unsupported Auto Profile setting")
    payload={"owner_id":str(owner_id),"updated_at":datetime.now(timezone.utc).isoformat()}
    payload.update({key:str(value or "") for key,value in updates.items()})
    try:
        response=requests.post(_url(),params={"on_conflict":"owner_id"},headers=_headers("resolution=merge-duplicates,return=representation"),json=payload,timeout=30)
        response.raise_for_status(); rows=response.json()
    except (requests.RequestException,ValueError) as exc:
        raise AutoProfileStoreError("Could not save Auto Profile settings") from exc
    if not isinstance(rows,list) or not rows:
        raise AutoProfileStoreError("Auto Profile settings were not saved")
    return rows[0]
