from datetime import datetime, timedelta
from bson import ObjectId
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv
from passlib.context import CryptContext

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "ai_chatbot_db")

client = AsyncIOMotorClient(MONGO_URI)
db = client[DB_NAME]
users_collection = db["users"]
history_collection = db["chat_history"]
faq_collection = db["faqs"]
config_collection = db["config"]
profile_collection = db["profile"]
integrations_collection = db["integrations"]
admins_collection = db["admins"]
otp_collection = db["otps"]
knowledge_collection = db["knowledge"]

async def add_knowledge(filename, content):
    await knowledge_collection.insert_one({
        "filename": filename,
        "content": content,
        "timestamp": datetime.utcnow()
    })

async def get_all_knowledge():
    cursor = knowledge_collection.find()
    return await cursor.to_list(length=100)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

async def get_db():
    return db

async def save_chat_history(platform, user_id, message, response, username=None, avatar_url=None):
    history_collection = db["chat_history"]
    chat_entry = {
        "platform": platform,
        "user_id": str(user_id),
        "message": message,
        "response": response,
        "timestamp": datetime.utcnow()
    }
    if username: chat_entry["username"] = username
    if avatar_url: chat_entry["avatar_url"] = avatar_url
    await history_collection.insert_one(chat_entry)

async def get_user_context(platform, user_id, limit=5):
    cursor = history_collection.find({"platform": platform, "user_id": str(user_id)}).sort("timestamp", -1).limit(limit)
    history = await cursor.to_list(length=limit)
    return history[::-1]

async def get_human_takeover_status(user_id):
    user = await users_collection.find_one({"user_id": str(user_id)})
    return user.get("is_human_taking_over", False) if user else False

async def set_human_takeover_status(user_id, status):
    await users_collection.update_one(
        {"user_id": str(user_id)},
        {"$set": {"is_human_taking_over": status}},
        upsert=True
    )

async def get_active_conversations(limit=20):
    # Aggregate to get unique users and their last message
    pipeline = [
        {"$sort": {"timestamp": -1}},
        {"$group": {
            "_id": "$user_id",
            "last_message": {"$first": "$message"},
            "timestamp": {"$first": "$timestamp"},
            "platform": {"$first": "$platform"},
            "username": {"$first": "$username"},
            "avatar_url": {"$first": "$avatar_url"}
        }},
        {"$sort": {"timestamp": -1}},
        {"$limit": limit}
    ]
    cursor = history_collection.aggregate(pipeline)
    return await cursor.to_list(length=limit)

async def get_faqs():
    cursor = faq_collection.find()
    return await cursor.to_list(length=100)

async def add_faq(question, answer):
    await faq_collection.insert_one({
        "question": question,
        "answer": answer,
        "timestamp": datetime.utcnow()
    })

async def update_faq(faq_id, question, answer):
    await faq_collection.update_one(
        {"_id": ObjectId(faq_id)},
        {"$set": {"question": question, "answer": answer}}
    )

async def delete_faq(faq_id):
    await faq_collection.delete_one({"_id": ObjectId(faq_id)})

async def get_ai_config():
    config = await config_collection.find_one({"type": "global_settings"})
    if not config:
        return {
            "engine": "gpt-4o", 
            "provider": "openai", 
            "system_prompt": "You are Pulse AI, a professional and high-performance AI assistant.",
            "openai_assistant_id": None,
            "openai_vector_store_id": None
        }
    return config

async def update_ai_config(engine, provider, fallback_enabled=True, system_prompt=None, assistant_id=None, vector_store_id=None):
    update_data = {"engine": engine, "provider": provider, "fallback_enabled": fallback_enabled}
    if system_prompt: update_data["system_prompt"] = system_prompt
    if assistant_id: update_data["openai_assistant_id"] = assistant_id
    if vector_store_id: update_data["openai_vector_store_id"] = vector_store_id
        
    await config_collection.update_one(
        {"type": "global_settings"},
        {"$set": update_data},
        upsert=True
    )

async def add_notification(platform, user_id, text):
    await db.notifications.insert_one({
        "platform": platform,
        "user_id": user_id,
        "text": text,
        "timestamp": datetime.utcnow(),
        "read": False
    })

async def get_notifications(limit=20):
    cursor = db.notifications.find().sort("timestamp", -1).limit(limit)
    notifs = await cursor.to_list(length=limit)
    for n in notifs: n["_id"] = str(n["_id"])
    return notifs

async def clear_notifications():
    await db.notifications.delete_many({})

async def get_user_thread(platform, user_id):
    user = await users_collection.find_one({"platform": platform, "user_id": str(user_id)})
    return user.get("openai_thread_id") if user else None

async def save_user_thread(platform, user_id, thread_id):
    await users_collection.update_one(
        {"platform": platform, "user_id": str(user_id)},
        {"$set": {"openai_thread_id": thread_id}},
        upsert=True
    )

async def get_admin_profile(email: str):
    admin = await admins_collection.find_one({"email": email})
    if not admin:
        return {"name": "Pulse Admin", "email": email}
    return {
        "name": admin.get("name", "Pulse Admin"),
        "email": admin.get("email", email),
        "avatar_url": admin.get("avatar_url", ""),
        "role": admin.get("role", "SUPER ADMIN")
    }

async def update_admin_profile(current_email: str, name: str, new_email: str, avatar_url: str = None):
    update_data = {"name": name, "email": new_email}
    if avatar_url:
        update_data["avatar_url"] = avatar_url
    await admins_collection.update_one(
        {"email": current_email},
        {"$set": update_data}
    )

async def get_admin_preferences(email: str):
    admin = await admins_collection.find_one({"email": email})
    if admin and "preferences" in admin:
        return admin["preferences"]
    return {"notifications": True, "auditLog": False}

async def update_admin_preferences(email: str, notifications: bool, auditLog: bool):
    await admins_collection.update_one(
        {"email": email},
        {"$set": {"preferences": {"notifications": notifications, "auditLog": auditLog}}}
    )


async def get_integration_status():
    integrations = await integrations_collection.find().to_list(length=10)
    if not integrations:
        return [
            {"name": "Master AI Switch", "status": "CONNECTED", "platform": "global"},
            {"name": "WhatsApp", "status": "CONNECTED", "platform": "whatsapp"},
            {"name": "Telegram", "status": "CONNECTED", "platform": "telegram"},
            {"name": "Discord", "status": "CONNECTED", "platform": "discord"}
        ]
    
    # Sanitize for JSON (convert ObjectId to string)
    for i in integrations:
        if "_id" in i:
            i["_id"] = str(i["_id"])
            
    return integrations

async def update_integration_status(platform, status):
    await integrations_collection.update_one(
        {"platform": platform},
        {"$set": {"status": status}},
        upsert=True
    )

async def is_platform_active(platform):
    # Check Global switch first
    global_int = await integrations_collection.find_one({"platform": "global"})
    if global_int and global_int.get("status") == "DISCONNECTED":
        return False
        
    # Check specific platform switch
    plat_int = await integrations_collection.find_one({"platform": platform})
    if plat_int and plat_int.get("status") == "DISCONNECTED":
        return False
        
    return True

# --- Auth Functions ---
def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password: str, hashed_password: str):
    return pwd_context.verify(plain_password, hashed_password)

async def get_admin_user(email: str):
    return await admins_collection.find_one({"email": email})

async def update_admin_password(email: str, new_password: str):
    await admins_collection.update_one(
        {"email": email},
        {"$set": {"password": hash_password(new_password)}}
    )

async def track_failed_login(email: str):
    admin = await admins_collection.find_one({"email": email})
    if not admin:
        return
    
    attempts = admin.get("failed_login_attempts", 0) + 1
    update_data = {"failed_login_attempts": attempts}
    
    # Lock account after 5 failed attempts
    if attempts >= 5:
        lockout_duration = 15 # 15 minutes lockout
        update_data["lockout_until"] = datetime.utcnow() + timedelta(minutes=lockout_duration)
        print(f"🔒 Account locked: {email} for {lockout_duration} mins")
    
    await admins_collection.update_one({"email": email}, {"$set": update_data})

async def reset_failed_login(email: str):
    await admins_collection.update_one(
        {"email": email}, 
        {"$set": {"failed_login_attempts": 0}, "$unset": {"lockout_until": ""}}
    )

async def is_account_locked(email: str):
    admin = await admins_collection.find_one({"email": email})
    if not admin:
        return False, None
    
    lockout_until = admin.get("lockout_until")
    if lockout_until and datetime.utcnow() < lockout_until:
        return True, lockout_until
    
    return False, None

async def save_otp(email: str, otp: str):
    # Expire in 10 minutes
    expires_at = datetime.utcnow() + timedelta(minutes=10)
    await otp_collection.update_one(
        {"email": email},
        {"$set": {"otp": otp, "expires_at": expires_at}},
        upsert=True
    )

async def verify_otp(email: str, otp: str):
    record = await otp_collection.find_one({"email": email})
    if not record:
        return False
    if record["otp"] != otp:
        return False
    if datetime.utcnow() > record["expires_at"]:
        return False
    # Clear after use
    await otp_collection.delete_one({"email": email})
    return True

async def create_admin(email, password):
    # Check if exists
    existing = await admins_collection.find_one({"email": email})
    if existing:
        return False
    
    admin_user = {
        "email": email,
        "password": hash_password(password),
        "created_at": datetime.utcnow(),
        "is_2fa_enabled": False,
        "role": "SUPER ADMIN"
    }
    await admins_collection.insert_one(admin_user)
    return True

async def create_initial_admin():
    # Only create if no admins exist
    count = await admins_collection.count_documents({})
    if count == 0:
        admin_user = {
            "email": "admin@pulseai.com",
            "password": hash_password("admin123"), # Default password
            "created_at": datetime.utcnow(),
            "is_2fa_enabled": False,
            "role": "SUPER ADMIN"
        }
        await admins_collection.insert_one(admin_user)
        print("Initial admin created: admin@pulseai.com / admin123")
