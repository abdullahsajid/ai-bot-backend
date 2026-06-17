import os
import logging
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
from ..ai_engine import ai_engine
from ..database import save_chat_history, get_user_context, get_human_takeover_status, get_faqs, get_all_knowledge, db

load_dotenv()

async def telegram_security_announcement_loop(bot):
    print("🚀 [TELEGRAM] Starting security announcement background loop...")
    # Wait a few seconds to let everything settle
    await asyncio.sleep(5)
    
    settings = db["system_settings"]
    
    security_text = (
        "⚠️ Security Reminder from PulseAI\n\n"
        "Lumo Wallet will never ask you for:\n\n"
        "• Private Keys\n"
        "• Recovery Phrases\n"
        "• Deposits\n"
        "• Wallet Transfers\n"
        "• Bank Transfers\n\n"
        "If anyone contacts you claiming to represent Lumo Wallet and asks for any of the above, it is a scam.\n\n"
        "Stay safe. Stay in control.\n\n"
        "💜 One Wallet. Endless Possibilities.\n\n"
        "#LumoWallet #PulseAI #CryptoSecurity #SelfCustody #StaySafe"
    )
    
    image_path = os.path.join(os.path.dirname(__file__), "assets", "security_alert.png")
    
    while True:
        try:
            doc = await settings.find_one({"key": "last_security_announcement_telegram"})
            now = datetime.utcnow()
            should_send = False
            
            if not doc:
                should_send = True
            else:
                last_sent = doc.get("timestamp")
                if not last_sent or (now - last_sent).total_seconds() >= 18000:
                    should_send = True
                    
            if should_send:
                # Fetch target groups from env
                groups_str = os.getenv("TELEGRAM_SECURITY_GROUPS")
                if not groups_str:
                    # Fallback to ALLOWED_TELEGRAM_GROUPS
                    groups_str = os.getenv("ALLOWED_TELEGRAM_GROUPS")
                    
                if groups_str:
                    group_ids = [g.strip() for g in groups_str.split(",") if g.strip()]
                    sent_any = False
                    for gid in group_ids:
                        try:
                            if os.path.exists(image_path):
                                with open(image_path, "rb") as photo_file:
                                    await bot.send_photo(
                                        chat_id=gid,
                                        photo=photo_file,
                                        caption=security_text
                                    )
                            else:
                                await bot.send_message(
                                    chat_id=gid,
                                    text=security_text
                                )
                            print(f"✅ [TELEGRAM] Sent security announcement to group {gid}")
                            sent_any = True
                        except Exception as e:
                            print(f"❌ [TELEGRAM] Failed to send to group {gid}: {e}")
                            
                    if sent_any:
                        await settings.update_one(
                            {"key": "last_security_announcement_telegram"},
                            {"$set": {"timestamp": now}},
                            upsert=True
                        )
                else:
                    print("⚠️ [TELEGRAM] No TELEGRAM_SECURITY_GROUPS or ALLOWED_TELEGRAM_GROUPS configured.")
        except Exception as e:
            print(f"❌ [TELEGRAM] Error in security announcement loop: {e}")
            
        # Sleep for 10 minutes before checking again
        await asyncio.sleep(600)

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

    # If replying to another human in a group and not explicitly mentioned, ignore it
    if is_group and is_reply_to_other and not is_mentioned:
        print(f"⏩ [TELEGRAM] Ignoring reply to another user in group chat")
        return
    is_reply_to_other = update.message.reply_to_message and update.message.reply_to_message.from_user.id != bot_user.id


    
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

    # Base decision: Private chats, mentions, and replies ALWAYS trigger a response
    should_respond = not is_group or is_mentioned or is_reply_to_bot

    # Check dashboard "Mention Only" toggle
    if is_group:
        from ..database import get_ai_config
        config = await get_ai_config()
        mention_only = config.get("telegram_mention_only", False)
        
        if mention_only:
            # Toggle ON → only DM, mention, or reply-to-bot (no continuity)
            should_respond = not is_group or is_mentioned or is_reply_to_bot
        else:
            # Toggle OFF → DM, mention, reply-to-bot, AND continuity (but NO auto-intervention)
            should_respond = not is_group or is_mentioned or is_reply_to_bot or is_continuity

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

async def post_init(application):
    asyncio.create_task(telegram_security_announcement_loop(application.bot))

def run_telegram():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        print("TELEGRAM_TOKEN not found in environment variables.")
        return
    
    application = ApplicationBuilder().token(token).post_init(post_init).build()
    
    text_handler = MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message)
    application.add_handler(text_handler)
    
    print("Telegram bot is running...")
    application.run_polling()

if __name__ == "__main__":
    run_telegram()
