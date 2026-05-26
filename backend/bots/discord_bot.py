import os
import discord
from discord.ext import commands
import httpx
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
from ..ai_engine import ai_engine
from ..database import save_chat_history, get_user_context, get_human_takeover_status, get_faqs, get_all_knowledge, get_user_thread, save_user_thread

load_dotenv()

class MyDiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    async def on_message(self, message):
        # 1. Ignore yourself and other bots
        if message.author.bot:
            return

        channel_name = message.channel.name.lower() if message.guild else "DM"
        print(f"📩 [DISCORD] Message received in {channel_name} from {message.author}: {message.content[:50]}...")

        # 2. Ignore log and ticket channels/categories
        log_keywords = [
            "logs", "audit", "admin", "welcome", "rules", 
            "announcements", "alert", "start-here", "faq", "links", "verify","official-links","server-logs","discord-updates",
            "staff-announcements", "ticket"
        ]
        
        if message.guild:
            category_name = message.channel.category.name.lower() if message.channel.category else ""
            if any(key in channel_name for key in log_keywords) or any(key in category_name for key in log_keywords):
                print(f"⏩ [DISCORD] Ignoring system/log channel: {channel_name}")
                return

        # 3. Check for mentions/replies
        is_dm = message.guild is None
        is_directly_mentioned = self.user in message.mentions

        # If this is a reply to another human (not the bot), ignore it unless directly mentioned
        if message.guild and message.reference and not is_directly_mentioned:
            try:
                ref_msg = await message.channel.fetch_message(message.reference.message_id)
                if ref_msg and ref_msg.author.id != self.user.id and not ref_msg.author.bot:
                    print(f"⏩ [DISCORD] Ignoring reply to another human in #{message.channel.name}")
                    return
            except Exception:
                pass

        # Handle direct AI interaction
        user_id = str(message.author.id)
        channel_id_str = str(message.channel.id) if not is_dm else "DM"
        composite_id = f"{user_id}:{channel_id_str}"
        
        # Clean the message
        user_message = message.content
        if is_directly_mentioned:
            user_message = user_message.replace(f'<@!{self.user.id}>', '').replace(f'<@{self.user.id}>', '').strip()

        # 4. Smart Intervention / Continuity
        should_respond = is_dm or is_directly_mentioned
        
        if not should_respond and message.guild:
            print(f"🔍 [DISCORD] Checking continuity/intent for: {user_id}")
            # Check for Continuity
            is_continuity = False
            
            try:
                last_chats = await get_user_context("discord", composite_id, limit=1)
                if last_chats:
                    last_chat = last_chats[0]
                    if last_chat.get('response') and "[AI_DISABLED_OR_HUMAN_ACTIVE]" not in last_chat['response']:
                        last_ts = last_chat.get('timestamp')
                        if last_ts:
                            now = datetime.now(pytz.UTC)
                            if (now - last_ts.replace(tzinfo=pytz.UTC)) < timedelta(minutes=10):
                                is_continuity = True
                                print("✅ [DISCORD] Continuity detected (10m window)")
            except Exception as e:
                print(f"⚠️ [DISCORD] History check error: {e}")
            
            if is_continuity:
                should_respond = True
            else:
                # No smart intervention; only mention or DM triggers a response
                should_respond = False

        if not should_respond:
            return

        # 5. Check Active Status
        from ..database import is_platform_active
        is_active = await is_platform_active("discord")
        is_human = await get_human_takeover_status(composite_id)
        
        if not is_active or is_human:
            print(f"🚫 [DISCORD] AI Disabled or Human Takeover for {user_id}")
            await save_chat_history("discord", composite_id, user_message, "[AI_DISABLED_OR_HUMAN_ACTIVE]", username=message.author.name)
            return

        # 6. Generate response
        print(f"🧠 [DISCORD] Generating AI response for {user_id}...")
        try:
            context = await get_user_context("discord", composite_id)
            faqs = await get_faqs()
            knowledge = await get_all_knowledge()
            
            response = await ai_engine.generate_response("discord", composite_id, user_message, context, faqs=faqs, knowledge=knowledge)
            print(f"✅ [DISCORD] AI response generated ({len(response)} chars)")
        except Exception as e:
            print(f"❌ [DISCORD] CRITICAL ERROR: {e}")
            response = f"❌ DEBUG ERROR: {str(e)}"
            
        # 7. Send and Save
        try:
            if len(response) > 2000:
                for i in range(0, len(response), 1900):
                    chunk = response[i:i + 1900]
                    await message.reply(chunk) if i == 0 else await message.channel.send(chunk)
            else:
                await message.reply(response)
            
            username = message.author.display_name or message.author.name
            avatar_url = str(message.author.display_avatar.url) if message.author.display_avatar else None
            await save_chat_history("discord", composite_id, user_message, response, username=username, avatar_url=avatar_url)
            print(f"💾 [DISCORD] Interaction saved to DB")
        except Exception as e:
            print(f"❌ [DISCORD] Failed to send/save: {e}")
            
def run_discord():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not found in environment variables.")
        return
    bot = MyDiscordBot()
    bot.run(token)

if __name__ == "__main__":
    run_discord()
