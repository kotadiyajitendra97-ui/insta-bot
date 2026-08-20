import requests

def verify_instagram_cookie(cookie_value: str) -> dict:
    """
    Instagram sessionid cookie se profile fetch karke verify karta hai.
    Returns: {"success": bool, "username": str, "message": str}
    """
    if not cookie_value:
        return {"success": False, "username": None, "message": "Cookie is empty."}
    
    # Clean cookie string if needed
    cookie_value = cookie_value.strip()
    if not cookie_value.startswith("sessionid="):
        cookie_value = f"sessionid={cookie_value}"

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 293.0.0.30.115",
        "Accept": "*/*",
        "Cookie": cookie_value
    }

    try:
        # Instagram web/mobile endpoint to get current user info
        response = requests.get("https://i.instagram.com/api/v1/accounts/current_user/?edit=true", headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            user = data.get("user", {})
            username = user.get("username")
            if username:
                return {"success": True, "username": username, "message": f"Successfully verified as @{username}"}
        
        return {"success": False, "username": None, "message": "Invalid Cookie or session expired."}
    except Exception as e:
        return {"success": False, "username": None, "message": f"Error verifying cookie: {str(e)}"}#!/usin/env pyhon3                    post_url = f"{BASE_URL}/reel/{code}/" if code else ""
                            
