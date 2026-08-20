import requests

def verify_instagram_cookie(cookie_input: str) -> dict:
    """
    User dwara bheji gayi poori cookie string ko direct Instagram par verify karta hai.
    """
    if not cookie_input:
        return {"success": False, "username": None, "message": "Cookie is empty."}
    
    cookie_string = cookie_input.strip()

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_string
    }

    try:
        # Instagram web edit profile endpoint jo poori cookie/session accept karta hai
        response = requests.get("https://www.instagram.com/accounts/edit/?__a=1&__d=DIS", headers=headers, timeout=10)
        
        if response.status_code == 200:
            try:
                data = response.json()
                user_data = data.get("form_data", {})
                username = user_data.get("username")
                if username:
                    return {"success": True, "username": username, "message": f"Successfully verified as @{username}"}
            except:
                pass

        # Alternate check using ig_user_id from query if present, or fallback
        # Let's check with standard mobile API endpoint using the same cookie string
        headers_mob = {
            "User-Agent": "Instagram 293.0.0.30.115 Android",
            "Cookie": cookie_string
        }
        res2 = requests.get("https://i.instagram.com/api/v1/accounts/current_user/?edit=true", headers=headers_mob, timeout=10)
        if res2.status_code == 200:
            data = res2.json()
            user = data.get("user", {})
            username = user.get("username")
            if username:
                return {"success": True, "username": username, "message": f"Successfully verified as @{username}"}

        return {"success": False, "username": None, "message": "Invalid Cookie or session expired. Please copy fresh cookies."}
    except Exception as e:
        return {"success": False, "username": None, "message": f"Error: {str(e)}"}
