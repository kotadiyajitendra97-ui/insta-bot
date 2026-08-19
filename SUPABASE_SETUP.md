# Supabase setup

1. Supabase dashboard → SQL Editor kholo.
2. `supabase_setup.sql` ka poora content run karo.
3. Railway Variables mein ye add karo:

```text
SUPABASE_URL=https://YOUR_PROJECT.supabase.co
SUPABASE_SERVICE_KEY=YOUR_SERVICE_ROLE_OR_SECRET_KEY
```

`SUPABASE_SERVICE_KEY` ko Telegram, GitHub ya logs mein kabhi paste mat karo.

New and refreshed cookie records compact `name=value; name=value` format mein save hote hain.

## Multiple admins

Har authorized Telegram admin ki apni Telegram user ID `owner_id` mein save hoti hai. Isliye Saved Logins, account limit, cookie export, permanent logout aur post account selection har admin ke liye alag hain. Existing table aur `(owner_id, instagram_user_id)` unique constraint already is isolation ko support karte hain; koi nayi table ya SQL migration required nahi hai.
Purane encrypted records ke liye old `SESSION_ENCRYPTION_KEY` temporarily rakho.
Jab sab accounts fresh login/refresh ho jaayen, ise remove kar sakte ho.

Bot flow:
- Cookie login successful hone par account compact cookie header ke saath upsert hota hai.
- `💾 Saved Logins` button sab saved usernames dikhata hai.
- Username tap karne par saved cookie validate aur active hoti hai.
- Instagram session expire/revoke hone par fresh cookie login karna padega.
