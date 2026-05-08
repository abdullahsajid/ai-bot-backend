import os
import logging
import asyncio
from openai import AsyncOpenAI
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

# Setup OpenAI
openai_client = None
if os.getenv("OPENAI_API_KEY"):
    openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Setup Gemini
gemini_key = os.getenv("GEMINI_API_KEY")
if gemini_key:
    genai.configure(api_key=gemini_key)

class AIEngine:
    def __init__(self):
        self.openai_model = "gpt-5.4"
        self.gemini_model = "gemini-1.5-flash"
        self.preferred_provider = os.getenv("AI_PROVIDER", "openai").lower()
        self.fallback_enabled = False
        self.system_prompt = """You are Pulse AI, a professional and high-performance AI assistant for Lumo Wallet.
        
        ### LINK FORMATTING RULES:
        - If the platform is 'discord' or 'telegram', ALWAYS use clean hyperlinks. Format: [Link Title ↗](URL)
        - If the platform is 'whatsapp', use raw URLs because WhatsApp does not support hidden links. Format: Link Title: URL
        - Use emojis sparingly to maintain a premium feel.

        ### OFFICIAL LUMO WALLET LINKS:
        - Facebook: https://www.facebook.com/profile.php?id=61579835237998
        - Instagram: https://www.instagram.com/lumo_wallet/
        - TikTok: https://www.tiktok.com/@lumo_wallet
        - YouTube: https://www.youtube.com/@lumo_wallet
        - X (Twitter): https://x.com/LumoWallet
        - LinkedIn: https://www.linkedin.com/company/lumo-wallet/
        - Discord Community: https://discord.gg/nWFXgWng25
        - Telegram Community: https://t.me/mylumoapp
        """

    async def should_intervene(self, user_message):
        """Quickly decide if the AI should respond to a message in a group chat."""
        # Clean message for check
        msg = (user_message or "").strip()
        if not msg: return False

        prompt = f"""
        You are Pulse AI, a smart assistant for Lumo Wallet. 
        You are monitoring a group chat. 
        Decide if you should respond to the following message.
        Respond 'YES' if the message is a question or request related to:
        - Lumo Wallet, crypto, fees, transactions, or technical support.
        - Questions directed at an assistant or asking for help.
        Respond 'NO' if it's general social chatter, greetings, or unrelated to your services.
        
        Message: "{msg}"
        
        Decision (YES/NO):"""
        
        try:
            # Use OpenAI for intent detection since Gemini is not configured
            response = await openai_client.chat.completions.create(
                model="gpt-4o-mini", # Using mini for faster/cheaper intent checks
                messages=[
                    {"role": "system", "content": "Respond only 'YES' or 'NO'."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=5,
                temperature=0
            )
            decision = response.choices[0].message.content.strip().upper()
            return "YES" in decision
        except Exception as e:
            print(f"Intention Check Failed: {e}")
            # Fallback to keyword-based detection if AI fails
            keywords = ["lumo", "wallet", "swap", "fee", "transfer", "help", "support", "how to", "pulse"]
            return any(kw in msg.lower() for kw in keywords)

    async def generate_response(self, platform, user_id, user_message, context=None, faqs=None, knowledge=None, thread_id=None):
        # 1. Build Enriched System Prompt (RAG - Retrieval Augmented Generation)
        enriched_prompt = f"{self.system_prompt}\n\nCURRENT PLATFORM: {platform}\n"
        
        # Inject FAQs as high-priority context for Semantic Matching
        if faqs:
            enriched_prompt += "\n\n### OFFICIAL FREQUENTLY ASKED QUESTIONS (FAQs):\n"
            for faq in faqs:
                enriched_prompt += f"Q: {faq['question']}\nA: {faq['answer']}\n\n"
            enriched_prompt += "If a user's question matches any of the above FAQs (even if worded differently), use the official answer provided."

        # Inject Knowledge Base documents
        if knowledge:
            relevant_facts = []
            keywords = user_message.lower().split()
            for doc in knowledge:
                content = doc.get('content', '').lower()
                if any(word in content for word in keywords if len(word) > 3):
                    relevant_facts.append(doc.get('content'))
            
            if relevant_facts:
                enriched_prompt += "\n\n### ADDITIONAL CONTEXT FROM KNOWLEDGE BASE:\n"
                enriched_prompt += "\n---\n".join(relevant_facts[:5])
                enriched_prompt += "\n---\nUse the above documents for detailed context if the FAQs do not cover the user's query."

        print(f"🤖 Generating AI response for {platform}:{user_id}...")

        # 3. Call Provider
        if self.preferred_provider == "openai" and openai_client:
            return await self._generate_openai(user_message, context, enriched_prompt)
        else:
            return await self._generate_gemini(user_message, enriched_prompt, context)

    async def _generate_openai(self, user_message, context, prompt):
        try:
            # 1. Format Conversation History (Memory)
            history_text = "\n### CONVERSATION HISTORY (MEMORY):\n"
            if context:
                for entry in context:
                    history_text += f"User: {entry['message']}\nAI: {entry['response']}\n"
            
            # Combine history with the system prompt
            full_instructions = f"{prompt}\n{history_text}\nAlways remember your previous offers and respond contextually."

            # Using the official Responses API abstraction
            response = await openai_client.responses.create(
                model="gpt-5.4",
                tools=[{"type": "web_search_preview"}],
                input=user_message,
                instructions=full_instructions
            )

            # Extracting text from the Response object
            for item in response.output:
                if item.type == "message":
                    for content_item in item.content:
                        if content_item.type == "output_text":
                            return content_item.text
            
            return "AI responded but no text content was found."

        except Exception as e:
            # If 'responses' is not found, it means the library needs an update
            if "has no attribute 'responses'" in str(e):
                return "Error: Your 'openai' library is outdated. Please run 'pip install --upgrade openai'."
            
            err = str(e).lower()
            if "quota" in err or "429" in err:
                if gemini_key and self.fallback_enabled:
                    return await self._generate_gemini(user_message, prompt, context)
                return "⚠️ OpenAI Quota Exceeded."
            return f"Error (OpenAI): {str(e)}"

    async def _generate_gemini(self, user_message, prompt, context=None):
        if not gemini_key:
            return "Error: Gemini API key not configured."
        try:
            model = genai.GenerativeModel(self.gemini_model)
            full_prompt = f"System Instruction: {prompt}\n\n"
            if context:
                for entry in context:
                    full_prompt += f"User: {entry['message']}\nAI: {entry['response']}\n"
            full_prompt += f"User: {user_message}"
            
            response = model.generate_content(full_prompt)
            return response.text
        except Exception as e:
            return f"Error (Gemini): {str(e)}"

# Singleton
ai_engine = AIEngine()
