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
        if message.author == self.user:
            return

        if message.content.startswith("!"):
            await self.process_commands(message)
            return

        # Handle direct AI interaction
        user_id = str(message.author.id)
        channel_id_str = str(message.channel.id)
        composite_id = f"{user_id}:{channel_id_str}"
        user_message = message.content

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

        # 1. Check for human takeover
        is_human = await get_human_takeover_status(composite_id)
        
        if is_human:
            await save_chat_history("discord", composite_id, user_message, "[HUMAN_TAKOVER_ACTIVE]", username=username, avatar_url=avatar_url)
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
