import os
import discord
from discord.ext import commands
import httpx
from dotenv import load_dotenv
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

        # 1.5 Check Allowed Servers lock
        # allowed_servers = os.getenv("ALLOWED_DISCORD_SERVERS")
        # if allowed_servers and message.guild is not None:
        #     allowed_list = [s.strip() for s in allowed_servers.split(',')]
        #     if str(message.guild.id) not in allowed_list:
        #         print(f"Ignored message from unauthorized server: {message.guild.id}")
        #         return

        # 2. Ignore log channels (administrative and read-only channels)
        log_keywords = [
            "logs", "audit", "admin", "welcome", "rules", 
            "announcements", "alert", "start-here", "faq", "links", "verify","official-links","server-logs","discord-updates",
            "staff-announcements"
        ]
        channel_name = message.channel.name.lower()
        if any(key in channel_name for key in log_keywords):
            return

        # 3. Ignore if a human is replying to or tagging another human
        # (This stops the bot from interrupting admin-to-user conversations)
        
        # Check if natively replying to someone else
        if message.reference and getattr(message.reference, "resolved", None):
            if message.reference.resolved.author.id != self.user.id:
                return # They are replying to someone else
                
        # Check if manually tagging someone else (but not the bot)
        if len(message.mentions) > 0 and self.user not in message.mentions:
            return

        if message.content.startswith("!"):
            await self.process_commands(message)
            return

        # Handle direct AI interaction
        user_id = str(message.author.id)
        channel_id_str = str(message.channel.id)
        composite_id = f"{user_id}:{channel_id_str}"
        
        # Clean the message (remove the mention tag)
        user_message = message.content
        if self.user in message.mentions:
            user_message = user_message.replace(f'<@!{self.user.id}>', '').replace(f'<@{self.user.id}>', '').strip()

        # Capture user identity
        username = message.author.display_name or message.author.name
        avatar_url = str(message.author.display_avatar.url) if message.author.display_avatar else None

        # Notify Dashboard
        try:
            async with httpx.AsyncClient() as client:
                await client.post("http://localhost:8000/internal/notify", json={
                    "platform": "discord",
                    "user_id": composite_id,
                    "message": user_message
                })
        except Exception:
            pass

        # 1. Check for human takeover or global AI switch
        from ..database import is_platform_active
        is_active = await is_platform_active("discord")
        is_human = await get_human_takeover_status(composite_id)
        
        if not is_active or is_human:
            await save_chat_history("discord", composite_id, user_message, "[AI_DISABLED_OR_HUMAN_ACTIVE]", username=username, avatar_url=avatar_url)
            return

        # 2. Get context from DB
        context = await get_user_context("discord", composite_id)
        faqs = await get_faqs()
        knowledge = await get_all_knowledge()

        # 3. Generate response
        try:
            async with message.channel.typing():
                response = await ai_engine.generate_response("discord", composite_id, user_message, context, faqs=faqs, knowledge=knowledge)
        except Exception as e:
            print(f"Chat Error: {e}")
            response = "⚠️ I encountered an error while processing your request."
            
        # Discord limit is 2000 chars. Let's chunk the message.
        if len(response) > 2000:
            for i in range(0, len(response), 1900):
                chunk = response[i:i + 1900]
                await message.reply(chunk) if i == 0 else await message.channel.send(chunk)
        else:
            await message.reply(response)

        # Save to DB with identity
        await save_chat_history("discord", composite_id, user_message, response, username=username, avatar_url=avatar_url)

def run_discord():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not found in environment variables.")
        return
    bot = MyDiscordBot()
    bot.run(token)

if __name__ == "__main__":
    run_discord()
