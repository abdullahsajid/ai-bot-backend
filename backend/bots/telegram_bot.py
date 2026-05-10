import os
import logging
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
from ..ai_engine import ai_engine
from ..database import save_chat_history, get_user_context, get_human_takeover_status, get_faqs, get_all_knowledge

load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = str(user.id)
    user_message = update.message.text
    # Check Allowed Groups lock
    allowed_groups = os.getenv("ALLOWED_TELEGRAM_GROUPS")
    chat_id_str = str(update.effective_chat.id)
    if allowed_groups and update.effective_chat.type in ['group', 'supergroup']:
        allowed_list = [g.strip() for g in allowed_groups.split(',')]
        if chat_id_str not in allowed_list:
            print(f"Ignored message from unauthorized group: {chat_id_str}")
            return

    # 0. Check for Trigger Type
    is_group = update.effective_chat.type in ['group', 'supergroup']
    bot_user = await context.bot.get_me()
    is_mentioned = f"@{bot_user.username}" in (user_message or "")
    is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_user.id
    
    # 0.5 Contextual Continuity (Did the bot just speak?)
    is_continuity = False
    if is_group:
        from ..database import get_user_context
        
        last_chats = await get_user_context("telegram", chat_id_str, limit=1)
        if last_chats:
            last_chat = last_chats[0]
            # If the last thing in the DB was an AI response
            if last_chat.get('response') and "[AI_DISABLED_OR_HUMAN_ACTIVE]" not in last_chat['response']:
                last_ts = last_chat.get('timestamp')
                if last_ts:
                    # If it happened in the last 10 minutes, keep the conversation going
                    now = datetime.now(pytz.UTC)
                    if (now - last_ts.replace(tzinfo=pytz.UTC)) < timedelta(minutes=10):
                        is_continuity = True

    # Base decision: Private chats, mentions, replies, and continuity ALWAYS trigger a response
    should_respond = not is_group or is_mentioned or is_reply_to_bot or is_continuity

    # If in a group and NOT triggered, check if we should "Smart Intervene"
    if is_group and not should_respond:
        from ..database import get_ai_config
        config = await get_ai_config()
        # If "Mention Only" is ON, we strictly ignore non-mentions
        mention_only = config.get("telegram_mention_only", False)
        
        if not mention_only:
            # Check if the AI thinks it should jump in naturally
            should_respond = await ai_engine.should_intervene(user_message)
        
    if not should_respond:
        return # Ignore message if not triggered and not relevant

    # Capture user identity
    # Capture user identity (Format: Name (@username) or Name (ID))
    name = user.full_name or "Telegram User"
    if user.username:
        username = f"{name} (@{user.username})"
    else:
        username = f"{name} ({user_id})"
    avatar_url = None
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0:
            file = await photos.photos[0][0].get_file()
            avatar_url = file.file_path
    except Exception:
        pass

    # 1. Check for human takeover or global AI switch
    from ..database import is_platform_active
    chat_id_str = str(update.effective_chat.id)
    is_active = await is_platform_active("telegram")
    is_human = await get_human_takeover_status(chat_id_str)
    
    if not is_active or is_human:
        await save_chat_history("telegram", chat_id_str, user_message, "[AI_DISABLED_OR_HUMAN_ACTIVE]", username=username, avatar_url=avatar_url)
        return

    # 2. Get context from DB
    history_context = await get_user_context("telegram", chat_id_str)

    # 3. Send typing action
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass

    # 4. Generate response
    faqs = await get_faqs()
    knowledge = await get_all_knowledge()
    response = await ai_engine.generate_response("telegram", chat_id_str, user_message, history_context, faqs=faqs, knowledge=knowledge)
    
    # Telegram limit is 4096 chars.
    if len(response) > 4096:
        for i in range(0, len(response), 4000):
            await update.message.reply_text(response[i:i + 4000], parse_mode="Markdown")
    else:
        await update.message.reply_text(response, parse_mode="Markdown")

    # Save to DB with identity
    await save_chat_history("telegram", chat_id_str, user_message, response, username=username, avatar_url=avatar_url)
    
    # Notify Dashboard
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post("http://localhost:8000/internal/notify", json={
                "platform": "telegram",
                "user_id": chat_id_str,
                "message": user_message
            })
    except Exception:
        pass

def run_telegram():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_TOKEN not found in environment variables.")
        return
    
    application = ApplicationBuilder().token(token).build()
    
    text_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(text_handler)
    
    print("Telegram bot is running...")
    application.run_polling()

if __name__ == "__main__":
    run_telegram()
