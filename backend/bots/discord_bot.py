import os
import discord
from discord.ext import commands, tasks
import httpx
from dotenv import load_dotenv
from datetime import datetime, timedelta
import pytz
from ..ai_engine import ai_engine
from ..database import save_chat_history, get_user_context, get_human_takeover_status, get_faqs, get_all_knowledge, get_user_thread, save_user_thread, db

load_dotenv()

async def check_and_send_discord_announcement(bot):
    # Check last alert timestamp in database
    settings = db["system_settings"]
    doc = await settings.find_one({"key": "last_security_announcement_discord"})
    now = datetime.utcnow()
    
    if doc:
        last_sent = doc.get("timestamp")
        # Check if 24 hours have passed (86400 seconds)
        if last_sent and (now - last_sent).total_seconds() < 86400:
            return
            
    # Try deleting the bot's own previous announcement messages
    if doc:
        previous_messages = doc.get("sent_messages")
        if previous_messages:
            print(f"🗑️ [DISCORD] Attempting to delete {len(previous_messages)} previous security announcement(s)...")
            for prev in previous_messages:
                try:
                    chan_id = prev.get("channel_id")
                    msg_id = prev.get("message_id")
                    if chan_id and msg_id:
                        channel = bot.get_channel(chan_id)
                        if not channel:
                            channel = await bot.fetch_channel(chan_id)
                        if channel:
                            try:
                                msg = await channel.fetch_message(msg_id)
                                # Delete only this specific message
                                await msg.delete()
                                print(f"✅ [DISCORD] Deleted previous message {msg_id} in channel {chan_id}")
                            except Exception as e:
                                print(f"⚠️ [DISCORD] Could not delete message {msg_id} in channel {chan_id}: {e}")
                except Exception as e:
                    print(f"⚠️ [DISCORD] Error processing deletion for previous message {prev}: {e}")

    # Fetch configured channels from env
    channels_str = os.getenv("DISCORD_SECURITY_CHANNELS")
    channel_ids = []
    if channels_str:
        channel_ids = [int(c.strip()) for c in channels_str.split(",") if c.strip().isdigit()]
        
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
    
    target_channels = []
    if channel_ids:
        for cid in channel_ids:
            try:
                channel = bot.get_channel(cid)
                if not channel:
                    channel = await bot.fetch_channel(cid)
                if channel:
                    target_channels.append(channel)
                else:
                    print(f"❌ [DISCORD] Channel {cid} not found.")
            except Exception as e:
                print(f"❌ [DISCORD] Failed to fetch channel {cid}: {e}")
    else:
        # Fallback to text channels that match the category ID
        category_filter = os.getenv("DISCORD_SECURITY_CATEGORY", "1488623220239241316").strip()
        for guild in bot.guilds:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.send_messages:
                    if channel.category:
                        cat_id = str(channel.category.id)
                        cat_name = channel.category.name.lower()
                        if cat_id == category_filter or cat_name == category_filter.lower():
                            target_channels.append(channel)
                    
    if not target_channels:
        print("⚠️ [DISCORD] No target channels found to send the announcement.")
        return
        
    sent_any = False
    new_sent_messages = []
    for channel in target_channels:
        try:
            msg = None
            if os.path.exists(image_path):
                file = discord.File(image_path, filename="security_alert.png")
                msg = await channel.send(content=security_text, file=file)
            else:
                msg = await channel.send(content=security_text)
            
            print(f"✅ [DISCORD] Sent security announcement to channel {channel.name} ({channel.id})")
            sent_any = True
            if msg:
                new_sent_messages.append({"channel_id": channel.id, "message_id": msg.id})
        except Exception as e:
            print(f"❌ [DISCORD] Failed to send to channel {channel.id}: {e}")
            
    if sent_any:
        # Update last sent timestamp and new message mappings for future deletions
        await settings.update_one(
            {"key": "last_security_announcement_discord"},
            {"$set": {
                "timestamp": now,
                "sent_messages": new_sent_messages
            }},
            upsert=True
        )

async def check_and_send_lumo_security_notice(bot):
    settings = db["system_settings"]
    now = datetime.utcnow()
    
    # 1. Opposite Time Rule (Check last time PulseAI alert was sent)
    pulse_doc = await settings.find_one({"key": "last_security_announcement_discord"})
    if not pulse_doc:
        print("⚠️ [DISCORD] PulseAI alert has never been sent yet. Delaying Lumo notice for alignment.")
        return
        
    pulse_sent = pulse_doc.get("timestamp")
    if not pulse_sent:
        print("⚠️ [DISCORD] PulseAI alert has no timestamp. Delaying Lumo notice for alignment.")
        return
        
    # Must be at least 12 hours since last PulseAI alert
    if (now - pulse_sent).total_seconds() < 43200:
        return

    # 2. Check last Lumo alert timestamp in database (Should only send once every 24h)
    doc = await settings.find_one({"key": "last_lumo_security_notice_discord"})
    if doc:
        last_sent = doc.get("timestamp")
        if last_sent and (now - last_sent).total_seconds() < 86400:
            return
            
    # Try deleting previous Lumo announcement messages
    if doc:
        previous_messages = doc.get("sent_messages")
        if previous_messages:
            print(f"🗑️ [DISCORD] Attempting to delete {len(previous_messages)} previous Lumo security notice(s)...")
            for prev in previous_messages:
                try:
                    chan_id = prev.get("channel_id")
                    msg_id = prev.get("message_id")
                    if chan_id and msg_id:
                        channel = bot.get_channel(chan_id)
                        if not channel:
                            channel = await bot.fetch_channel(chan_id)
                        if channel:
                            try:
                                msg = await channel.fetch_message(msg_id)
                                await msg.delete()
                                print(f"✅ [DISCORD] Deleted previous Lumo message {msg_id} in channel {chan_id}")
                            except Exception as e:
                                print(f"⚠️ [DISCORD] Could not delete Lumo message {msg_id} in channel {chan_id}: {e}")
                except Exception as e:
                    print(f"⚠️ [DISCORD] Error processing deletion for previous Lumo message {prev}: {e}")

    # Fetch configured channels from env
    channels_str = os.getenv("DISCORD_SECURITY_CHANNELS")
    channel_ids = []
    if channels_str:
        channel_ids = [int(c.strip()) for c in channels_str.split(",") if c.strip().isdigit()]
        
    security_text = (
        "🔒 **Important Security Notice from Lumo Wallet**\n\n"
        "Dear Lumo Wallet community,\n\n"
        "We want to make our position clear regarding recent misinformation circulating online involving unauthorized use of the Lumo Wallet name and branding.\n\n"
        "Lumo Wallet **does not have an official token** and we have not created, launched, or endorsed any cryptocurrency token. Any token or project claiming to be affiliated with Lumo Wallet is not authorized by us.\n\n"
        "To help protect our users and provide greater transparency, we have added a **security notice on our official website**. This notice links to a detailed post explaining:\n\n"
        "✅ **The services Lumo Wallet does provide**\n"
        "• On/off-ramp solutions\n"
        "• Swap services\n"
        "• Card services\n"
        "• Staking services\n"
        "• Secure wallet infrastructure\n\n"
        "❌ **The services Lumo Wallet does not provide**\n"
        "• Token creation or issuance\n"
        "• Third-party token endorsements\n"
        "• Investment schemes or guaranteed returns\n"
        "• Requests for private keys or recovery phrases\n\n"
        "We encourage everyone to always verify information through our official channels and remain cautious when interacting with any crypto-related project.\n\n"
        "Before using any digital asset service or investing in any token, always do your own research, verify sources, and understand the risks involved.\n\n"
        "Your security and trust remain our priority.\n\n"
        "Stay safe,\n"
        "**The Lumo Wallet Team**"
    )
    
    image_path = os.path.join(os.path.dirname(__file__), "assets", "security-notice.png")
    
    target_channels = []
    if channel_ids:
        for cid in channel_ids:
            try:
                channel = bot.get_channel(cid)
                if not channel:
                    channel = await bot.fetch_channel(cid)
                if channel:
                    target_channels.append(channel)
                else:
                    print(f"❌ [DISCORD] Channel {cid} not found.")
            except Exception as e:
                print(f"❌ [DISCORD] Failed to fetch channel {cid}: {e}")
    else:
        # Fallback category category text channels
        category_filter = os.getenv("DISCORD_SECURITY_CATEGORY", "1488623220239241316").strip()
        for guild in bot.guilds:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if perms.send_messages:
                    if channel.category:
                        cat_id = str(channel.category.id)
                        cat_name = channel.category.name.lower()
                        if cat_id == category_filter or cat_name == category_filter.lower():
                            target_channels.append(channel)
                    
    if not target_channels:
        print("⚠️ [DISCORD] No target channels found to send the Lumo Wallet announcement.")
        return
        
    sent_any = False
    new_sent_messages = []
    for channel in target_channels:
        try:
            msg = None
            if os.path.exists(image_path):
                file = discord.File(image_path, filename="security-notice.png")
                msg = await channel.send(content=security_text, file=file)
            else:
                msg = await channel.send(content=security_text)
            
            print(f"✅ [DISCORD] Sent Lumo Wallet security announcement to channel {channel.name} ({channel.id})")
            sent_any = True
            if msg:
                new_sent_messages.append({"channel_id": channel.id, "message_id": msg.id})
        except Exception as e:
            print(f"❌ [DISCORD] Failed to send Lumo notice to channel {channel.id}: {e}")
            
    if sent_any:
        await settings.update_one(
            {"key": "last_lumo_security_notice_discord"},
            {"$set": {
                "timestamp": now,
                "sent_messages": new_sent_messages
            }},
            upsert=True
        )

class MyDiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        self.security_announcement_task.start()
        self.lumo_security_notice_task.start()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    @tasks.loop(minutes=10)
    async def security_announcement_task(self):
        try:
            await check_and_send_discord_announcement(self)
        except Exception as e:
            print(f"❌ [DISCORD] Error in security_announcement_task: {e}")

    @security_announcement_task.before_loop
    async def before_security_announcement_task(self):
        await self.wait_until_ready()

    @tasks.loop(minutes=10)
    async def lumo_security_notice_task(self):
        try:
            await check_and_send_lumo_security_notice(self)
        except Exception as e:
            print(f"❌ [DISCORD] Error in lumo_security_notice_task: {e}")

    @lumo_security_notice_task.before_loop
    async def before_lumo_security_notice_task(self):
        await self.wait_until_ready()

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

        # 4. Smart Intervention / Continuity (Disabled for now: only mention or DM triggers a response)
        should_respond = is_dm or is_directly_mentioned
        
        # if not should_respond and message.guild:
        #     print(f"🔍 [DISCORD] Checking continuity/intent for: {user_id}")
        #     # Check for Continuity
        #     is_continuity = False
        #     
        #     try:
        #         last_chats = await get_user_context("discord", composite_id, limit=1)
        #         if last_chats:
        #             last_chat = last_chats[0]
        #             if last_chat.get('response') and "[AI_DISABLED_OR_HUMAN_ACTIVE]" not in last_chat['response']:
        #                 last_ts = last_chat.get('timestamp')
        #                 if last_ts:
        #                     now = datetime.now(pytz.UTC)
        #                     if (now - last_ts.replace(tzinfo=pytz.UTC)) < timedelta(minutes=10):
        #                         is_continuity = True
        #                         print("✅ [DISCORD] Continuity detected (10m window)")
        #     except Exception as e:
        #         print(f"⚠️ [DISCORD] History check error: {e}")
        #     
        #     if is_continuity:
        #         should_respond = True
        #     else:
        #         # No smart intervention; only mention or DM triggers a response
        #         should_respond = False

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
