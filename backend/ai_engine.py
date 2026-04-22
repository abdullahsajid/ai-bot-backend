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
        self.openai_model = "gpt-4o"
        self.gemini_model = "gemini-1.5-flash"
        self.preferred_provider = os.getenv("AI_PROVIDER", "openai").lower()
        self.fallback_enabled = False
        self.system_prompt = "You are Pulse AI, a professional and high-performance AI assistant."

    async def generate_response(self, platform, user_id, user_message, context=None, faqs=None, knowledge=None, thread_id=None):
        # 1. Check FAQs first (Instant match)
        if faqs:
            query = user_message.lower().strip().strip('?')
            for faq in faqs:
                if faq['question'].lower().strip().strip('?') in query or query in faq['question'].lower():
                    return faq['answer']

        # 2. Build Enriched System Prompt (Manual RAG)
        enriched_prompt = self.system_prompt
        print(f"🤖 Generating response for {platform}:{user_id} using prompt: {enriched_prompt[:40]}...")
        if knowledge:
            relevant_facts = []
            keywords = user_message.lower().split()
            # Find the most relevant chunks from your uploaded docs
            for doc in knowledge:
                content = doc.get('content', '').lower()
                # If message contains words from the document, include that document
                if any(word in content for word in keywords if len(word) > 3):
                    relevant_facts.append(doc.get('content'))
            
            if relevant_facts:
                enriched_prompt += "\n\n### ADDITIONAL CONTEXT FROM YOUR DOCUMENTS:\n"
                enriched_prompt += "\n---\n".join(relevant_facts[:3]) # Take top 3 relevant docs
                enriched_prompt += "\n---\nUse the above context to answer accurately."

        # 3. Call Provider
        if self.preferred_provider == "openai" and openai_client:
            return await self._generate_openai(user_message, context, enriched_prompt)
        else:
            return await self._generate_gemini(user_message, enriched_prompt, context)

    async def _generate_openai(self, user_message, context, prompt):
        try:
            messages = [{"role": "system", "content": prompt}]
            
            # Add conversation history
            if context:
                for entry in context:
                    messages.append({"role": "user", "content": entry["message"]})
                    messages.append({"role": "assistant", "content": entry["response"]})
            
            messages.append({"role": "user", "content": user_message})

            response = await openai_client.chat.completions.create(
                model=self.openai_model,
                messages=messages,
                temperature=0.7
            )
            return response.choices[0].message.content
        except Exception as e:
            err = str(e).lower()
            if "quota" in err or "429" in err:
                if gemini_key and self.fallback_enabled:
                    return await self._generate_gemini(user_message, prompt, context)
                return "⚠️ OpenAI Quota Exceeded. Please check billing or enable Gemini fallback."
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
