import threading
from worker import background_worker
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from supabase_store import get_settings, save_settings, clear_account_settings
from auto_video_store import get_links, add_link, delete_link, clear_all_links
from insta_auto import verify_instagram_cookie

# Logging setup
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# Main Menu Keyboard
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔑 Instagram Cookie (Login)", callback_data="menu_cookie")],
        [InlineKeyboardButton("🔗 Manage Video Links (Max 50)", callback_data="menu_links")],
        [InlineKeyboardButton("🖼️ Auto Cover Photo (Thumbnail)", callback_data="menu_thumbnail")],
        [InlineKeyboardButton("✍️ Auto Caption", callback_data="menu_caption")],
        [InlineKeyboardButton("👤 Profile (DP & Bio)", callback_data="menu_profile")],
        [InlineKeyboardButton("🗑️ Clear / Delete Account Data", callback_data="menu_clear")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    settings = get_settings(user_id)
    username = settings.get("ig_username")
    
    status_text = f"Connected Account: @{username}" if username else "No Account Connected"
    
    welcome_msg = (
        f"🤖 *Instagram Automation Bot*\n\n"
        f"Status: *{status_text}*\n\n"
        f"Neeche diye gaye buttons se aap apne account, links, captions aur media manage kar sakte hain:"
    )
    
    if update.callback_query:
        await update.callback_query.message.edit_text(welcome_msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")
    else:
        await update.message.reply_text(welcome_msg, reply_markup=main_menu_keyboard(), parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = str(query.from_user.id)

    if data == "main_menu":
        await start(update, context)
        
    elif data == "menu_cookie":
        context.user_data["waiting_for"] = "cookie"
        text = "🔑 *Instagram Cookie (sessionid) Set karein*\n\nApni Instagram `sessionid` cookie yahan bhejye. Bot verify karke account name save kar lega."
        keyboard = [[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "menu_links":
        links = get_links(user_id)
        text = f"🔗 *Video Links Management* (Total: {len(links)}/50)\n\nAapke saved links:\n"
        for i, l in enumerate(links[:10], 1): # Show first 10
            text += f"{i}. {l['video_link']}\n"
        
        keyboard = [
            [InlineKeyboardButton("➕ Add New Link", callback_data="add_link_prompt")],
            [InlineKeyboardButton("🗑️ Delete All Links", callback_data="clear_links")],
            [InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "add_link_prompt":
        context.user_data["waiting_for"] = "video_link"
        text = "➕ *Add Video Link*\n\nApne public Telegram channel ki video link yahan bhejye:"
        keyboard = [[InlineKeyboardButton("« Back", callback_data="menu_links")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "clear_links":
        clear_all_links(user_id)
        await query.message.edit_text("✅ Saare video links delete kar diye gaye hain.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_links")]]))

    elif data == "menu_thumbnail":
        context.user_data["waiting_for"] = "thumbnail"
        text = "🖼️ *Auto Cover Photo (Thumbnail)*\n\nAap ek photo bhejiye, bot use har reel ke cover photo ke liye save kar lega."
        keyboard = [[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "menu_caption":
        context.user_data["waiting_for"] = "caption"
        text = "✍️ *Auto Caption*\n\nJo caption aapko har post par chahiye, wo text yahan bhejye. (Clear karne ke liye 'CLEAR' likh kar bhejiye)."
        keyboard = [[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "menu_profile":
        text = "👤 *Profile Settings (DP & Bio)*\n\nApni Profile Picture ya Bio set karne ke liye niche option choose karein:"
        keyboard = [
            [InlineKeyboardButton("📷 Set DP (Send Photo)", callback_data="set_dp_prompt")],
            [InlineKeyboardButton("📝 Set Bio & Link", callback_data="set_bio_prompt")],
            [InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif data == "set_dp_prompt":
        context.user_data["waiting_for"] = "dp"
        await query.message.edit_text("📷 Apni DP ke liye photo bhejye:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_profile")]]))

    elif data == "set_bio_prompt":
        context.user_data["waiting_for"] = "bio"
        await query.message.edit_text("📝 Apne Bio ka text aur link bhejye (Format: Bio Text | Link):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_profile")]]))

    elif data == "menu_clear":
        clear_account_settings(user_id)
        clear_all_links(user_id)
        await query.message.edit_text("🗑️ Aapka account data aur saari settings clear/delete kar di gayi hain.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Menu", callback_data="main_menu")]]))

async def handle_incoming_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    waiting_for = context.user_data.get("waiting_for")
    
    if not waiting_for:
        return

    text = update.message.text
    photo = update.message.photo

    if waiting_for == "cookie":
        res = verify_instagram_cookie(text)
        if res["success"]:
            save_settings(user_id, ig_cookie=text, ig_username=res["username"])
            await update.message.reply_text(f"✅ {res['message']}\nAccount successfully saved!")
        else:
            await update.message.reply_text(f"❌ {res['message']}")
        context.user_data["waiting_for"] = None
        await start(update, context)

    elif waiting_for == "video_link":
        links = get_links(user_id)
        if len(links) >= 50:
            await update.message.reply_text("❌ Limit reached! Aap maximum 50 links hi save kar sakte hain.")
        else:
            add_link(user_id, text)
            await update.message.reply_text(f"✅ Link successfully saved! (Total links: {len(links)+1}/50)")
        context.user_data["waiting_for"] = None
        await start(update, context)

    elif waiting_for == "caption":
        if text.upper() == "CLEAR":
            save_settings(user_id, auto_caption="")
            await update.message.reply_text("✅ Auto caption cleared!")
        else:
            save_settings(user_id, auto_caption=text)
            await update.message.reply_text("✅ Auto caption updated successfully!")
        context.user_data["waiting_for"] = None
        await start(update, context)

    elif waiting_for == "thumbnail" and photo:
        file_id = photo[-1].file_id
        save_settings(user_id, thumbnail_file_id=file_id)
        await update.message.reply_text("✅ Thumbnail cover photo successfully saved!")
        context.user_data["waiting_for"] = None
        await start(update, context)

    elif waiting_for == "dp" and photo:
        file_id = photo[-1].file_id
        save_settings(user_id, dp_file_id=file_id)
        await update.message.reply_text("✅ Profile picture (DP) saved successfully!")
        context.user_data["waiting_for"] = None
        await start(update, context)

    elif waiting_for == "bio":
        save_settings(user_id, bio_text=text)
        await update.message.reply_text("✅ Bio updated successfully!")
        context.user_data["waiting_for"] = None
        await start(update, context)

def main():
    if not TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not found in environment variables.")
        return
    
    # Background worker ko thread mein start karein
    worker_thread = threading.Thread(target=background_worker, daemon=True)
    worker_thread.start()
    print("Background worker started inside web service!")
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT | filters.PHOTO & ~filters.COMMAND, handle_incoming_message))
    
    print("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
