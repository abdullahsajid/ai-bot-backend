import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()

def diagnose():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("❌ Error: GEMINI_API_KEY not found in .env")
        return

    genai.configure(api_key=key)
    
    print(f"--- Diagnosing Gemini API Key ---")
    try:
        models = genai.list_models()
        available_models = []
        print("✅ Connection Successful. Available Models:")
        for m in models:
            print(f" - {m.name} (Supports: {m.supported_generation_methods})")
            available_models.append(m.name)
        
        if not available_models:
            print("❌ No models found for this API key.")
        
    except Exception as e:
        print(f"❌ Error listing models: {str(e)}")
        print("\nPossible reasons:")
        print("1. The API key is invalid.")
        print("2. The key is restricted to a specific project that lacks Gemini access.")
        print("3. You are in a restricted region.")

if __name__ == "__main__":
    diagnose()
