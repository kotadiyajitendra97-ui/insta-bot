import requests

def verify_instagram_cookie(cookie_input: str) -> dict:
    """
    Instagram sessionid / cookies ko verify karta hai.
    """
    if not cookie_input:
        return {"success": False, "username": None, "message": "Cookie is empty."}
    
    cookie_input = cookie_input.strip()
    
    # Agar user ne sirf sessionid value di hai
    if "sessionid=" not in cookie_input and "=" not in cookie_input:
        cookie_string = f"sessionid={cookie_input}"
    else:
        cookie_string = cookie_input

    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 293.0.0.30.115",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie_string
    }

    try:
        # Try web profile info endpoint which is more reliable with cookies
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

        # Fallback endpoint
        res2 = requests.get("https://i.instagram.com/api/v1/accounts/current_user/?edit=true", headers=headers, timeout=10)
        if res2.status_code == 200:
            data = res2.json()
            user = data.get("user", {})
            username = user.get("username")
            if username:
                return {"success": True, "username": username, "message": f"Successfully verified as @{username}"}

        return {"success": False, "username": None, "message": "Invalid Cookie or session expired. Make sure to provide a valid active sessionid."}
    except Exception as e:
        return {"success": False, "username": None, "message": f"Error: {str(e)}"}
