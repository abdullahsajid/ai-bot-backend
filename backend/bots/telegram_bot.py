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
    chat_id_str = str(update.effective_chat.id)
    chat_type = update.effective_chat.type
    
    print(f"📩 [TELEGRAM] Message received in {chat_type} ({chat_id_str}) from {user.full_name}: {user_message[:50]}...")

    # Check Allowed Groups lock
    allowed_groups = os.getenv("ALLOWED_TELEGRAM_GROUPS")
    if allowed_groups and chat_type in ['group', 'supergroup']:
        allowed_list = [g.strip() for g in allowed_groups.split(',')]
        if chat_id_str not in allowed_list:
            print(f"⏩ [TELEGRAM] Ignoring unauthorized group: {chat_id_str}")
            return

    # 0. Check for Trigger Type
    is_group = chat_type in ['group', 'supergroup']
    bot_user = await context.bot.get_me()
    is_mentioned = f"@{bot_user.username}" in (user_message or "")
    is_reply_to_bot = update.message.reply_to_message and update.message.reply_to_message.from_user.id == bot_user.id
    is_reply_to_other = update.message.reply_to_message and update.message.reply_to_message.from_user.id != bot_user.id

    # If replying to another user in group and not explicitly mentioned, ignore it
    if is_group and is_reply_to_other and not is_mentioned:
        print(f"⏩ [TELEGRAM] Ignoring reply to another user in group chat")
        return
    
    # 0.5 Contextual Continuity (Did the bot just speak?)
    is_continuity = False
    if is_group:
        print(f"🔍 [TELEGRAM] Checking continuity for: {chat_id_str}")
        
        try:
            last_chats = await get_user_context("telegram", chat_id_str, limit=1)
            if last_chats:
                last_chat = last_chats[0]
                if last_chat.get('response') and "[AI_DISABLED_OR_HUMAN_ACTIVE]" not in last_chat['response']:
                    last_ts = last_chat.get('timestamp')
                    if last_ts:
                        now = datetime.now(pytz.UTC)
                        if (now - last_ts.replace(tzinfo=pytz.UTC)) < timedelta(minutes=10):
                            is_continuity = True
                            print("✅ [TELEGRAM] Continuity detected")
        except Exception as e:
            print(f"⚠️ [TELEGRAM] Context check error: {e}")

    # Base decision: Private chats, mentions, replies, and continuity ALWAYS trigger a response
    should_respond = not is_group or is_mentioned or is_reply_to_bot or is_continuity

    if is_group and not should_respond:
        from ..database import get_ai_config
        config = await get_ai_config()
        mention_only = config.get("telegram_mention_only", False)
        
        if not mention_only:
            should_respond = await ai_engine.should_intervene(user_message)
            print(f"🤖 [TELEGRAM] AI Intervention Decision: {should_respond}")
        
    if not should_respond:
        return

    # 1. Check for human takeover or global AI switch
    from ..database import is_platform_active
    is_active = await is_platform_active("telegram")
    is_human = await get_human_takeover_status(chat_id_str)
    
    if not is_active or is_human:
        print(f"🚫 [TELEGRAM] AI Disabled or Human Takeover for {chat_id_str}")
        await save_chat_history("telegram", chat_id_str, user_message, "[AI_DISABLED_OR_HUMAN_ACTIVE]")
        return

    # 2. Generate response
    print(f"🧠 [TELEGRAM] Generating AI response for {chat_id_str}...")
    try:
        history_context = await get_user_context("telegram", chat_id_str)
        faqs = await get_faqs()
        knowledge = await get_all_knowledge()
        response = await ai_engine.generate_response("telegram", chat_id_str, user_message, history_context, faqs=faqs, knowledge=knowledge)
        print(f"✅ [TELEGRAM] AI response generated ({len(response)} chars)")
    except Exception as e:
        print(f"❌ [TELEGRAM] CRITICAL ERROR: {e}")
        response = f"❌ DEBUG ERROR: {str(e)}"

    # 3. Send and Save
    try:
        if len(response) > 4096:
            for i in range(0, len(response), 4000):
                await update.message.reply_text(response[i:i + 4000], parse_mode="Markdown")
        else:
            await update.message.reply_text(response, parse_mode="Markdown")

        name = user.full_name or "Telegram User"
        username = f"{name} (@{user.username})" if user.username else f"{name} ({user_id})"
        await save_chat_history("telegram", chat_id_str, user_message, response, username=username)
        print(f"💾 [TELEGRAM] Interaction saved to DB")
    except Exception as e:
        print(f"❌ [TELEGRAM] Failed to send/save: {e}")
    
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
