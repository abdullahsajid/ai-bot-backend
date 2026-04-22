import os
import uvicorn
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel
from typing import List, Optional, Set
from fastapi import Form, File, UploadFile
from contextlib import asynccontextmanager
from .ai_engine import ai_engine, openai_client
from .database import (
    db, save_chat_history, get_user_context, get_active_conversations, 
    get_human_takeover_status, set_human_takeover_status, get_faqs, 
    add_faq, update_faq, delete_faq, get_ai_config, update_ai_config, get_admin_profile, 
    update_admin_profile, get_integration_status, update_integration_status, 
    get_admin_user, update_admin_password, verify_password, 
    create_initial_admin, create_admin, get_admin_preferences, 
    update_admin_preferences, save_otp, verify_otp, add_knowledge, get_all_knowledge,
    get_user_thread, save_user_thread, add_notification, get_notifications, clear_notifications
)
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from .bots.whatsapp import whatsapp_bot

# Security Settings
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key_here")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 1 day

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup Logic
    await create_initial_admin()
    
    config = await get_ai_config()
    if config:
        ai_engine.preferred_provider = config.get("provider", "openai")
        ai_engine.fallback_enabled = config.get("fallback_enabled", False)
        ai_engine.system_prompt = config.get("system_prompt", ai_engine.system_prompt)
        engine = config.get("engine", "gpt-4o")
        if "gemini" in engine.lower():
            ai_engine.gemini_model = engine
        elif "gpt" in engine.lower():
            ai_engine.openai_model = engine
    yield

app = FastAPI(title="AI Chatbot Management Dashboard", lifespan=lifespan)
START_TIME = datetime.utcnow()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")

# WebSocket Manager for broadcasting
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Models
class ChatEntry(BaseModel):
    platform: str
    user_id: str
    message: str
    response: str
    timestamp: Optional[datetime]

class TakeoverRequest(BaseModel):
    is_human: bool

class FAQRequest(BaseModel):
    question: str
    answer: str

class ConfigRequest(BaseModel):
    engine: str
    provider: str
    fallback_enabled: Optional[bool] = True
    system_prompt: Optional[str] = None

class ProfileRequest(BaseModel):
    name: str
    email: str

class IntegrationRequest(BaseModel):
    platform: str
    status: str

# Auth Utilities
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    return username

@app.post("/internal/notify")
async def internal_notify(data: dict):
    platform = data.get("platform")
    user_id = data.get("user_id")
    message = data.get("message")
    await add_notification(platform, user_id, message)
    await manager.broadcast({"type": "new_message", "platform": platform, "user_id": user_id, "message": message})
    return {"status": "sent"}

async def notify_dashboard(platform, user_id, message):
    await add_notification(platform, user_id, message)
    await manager.broadcast({"type": "new_message", "platform": platform, "user_id": user_id, "message": message})

@app.get("/notifications", dependencies=[Depends(get_current_user)])
async def fetch_notifications():
    return await get_notifications()

@app.delete("/notifications", dependencies=[Depends(get_current_user)])
async def wipe_notifications():
    await clear_notifications()
    return {"status": "success"}
# ... (rest of the file)


@app.get("/")
async def root():
    return {"status": "Dashboard API is running"}

@app.post("/auth/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = await get_admin_user(form_data.username) # Form 'username' field will contain email
    if not user or not verify_password(form_data.password, user["password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token = create_access_token(data={"sub": user["email"]})
    return {"access_token": access_token, "token_type": "bearer"}

class SignupRequest(BaseModel):
    email: str
    password: str

@app.post("/auth/signup")
async def signup(request: SignupRequest):
    success = await create_admin(request.email, request.password)
    if not success:
        raise HTTPException(status_code=400, detail="User with this email already exists")
    return {"status": "success", "message": "Admin account created"}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text() # Keep connection alive
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/internal/notify")
async def internal_notify(data: dict):
    # This allows separate bot processes to trigger dashboard updates
    await manager.broadcast({
        "type": "new_message",
        "platform": data.get("platform"),
        "user_id": data.get("user_id"),
        "message": data.get("message"),
        "response": data.get("response", "AI Processing..."),
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "ok"}

@app.get("/stats", dependencies=[Depends(get_current_user)])
async def get_stats(interval: str = "hourly"):
    history_collection = db["chat_history"]
    
    # 1. Total counts across all time
    discord_count = await history_collection.count_documents({"platform": {"$regex": "^discord$", "$options": "i"}})
    telegram_count = await history_collection.count_documents({"platform": {"$regex": "^telegram$", "$options": "i"}})
    whatsapp_count = await history_collection.count_documents({"platform": {"$regex": "^whatsapp$", "$options": "i"}})
    
    total_messages = discord_count + telegram_count + whatsapp_count
    if total_messages == 0:
        total_messages = await history_collection.count_documents({})

    # 2. Throughput Data
    now = datetime.utcnow()
    throughput = []
    
    if interval == "hourly":
        # Last 24 hours
        for i in range(23, -1, -1):
            start = now - timedelta(hours=i+1)
            end = now - timedelta(hours=i)
            count = await history_collection.count_documents({
                "timestamp": {"$gte": start, "$lt": end}
            })
            label = start.strftime("%H:00")
            throughput.append({"label": label, "value": count})
    else:
        # Last 7 days
        for i in range(6, -1, -1):
            start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            count = await history_collection.count_documents({
                "timestamp": {"$gte": start, "$lt": end}
            })
            label = start.strftime("%b %d")
            throughput.append({"label": label, "value": count})

    # 3. Active Users (Last 24h)
    last_24h = now - timedelta(hours=24)
    active_users = await history_collection.distinct("user_id", {"timestamp": {"$gte": last_24h}})
    active_users_count = len(active_users)

    # 4. Messages Today
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    messages_today = await history_collection.count_documents({"timestamp": {"$gte": day_start}})

    uptime_delta = datetime.utcnow() - START_TIME
    days = uptime_delta.days
    hours, remainder = divmod(uptime_delta.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    return {
        "total_messages": total_messages,
        "platform_stats": {
            "discord": discord_count,
            "telegram": telegram_count,
            "whatsapp": whatsapp_count
        },
        "throughput": throughput,
        "active_users_count": active_users_count,
        "messages_today": messages_today,
        "uptime": f"{days}d {hours}h {minutes}m",
        "stability": "99.98%"
    }

@app.get("/conversations", dependencies=[Depends(get_current_user)])
async def get_conversations():
    convos = await get_active_conversations()
    for c in convos:
        c["user_id"] = c.pop("_id")
    return convos

@app.get("/messages/{platform}/{user_id}", dependencies=[Depends(get_current_user)])
async def get_messages(platform: str, user_id: str, limit: int = 50):
    history_collection = db["chat_history"]
    cursor = history_collection.find({"platform": platform, "user_id": str(user_id)}).sort("timestamp", -1).limit(limit)
    messages = await cursor.to_list(length=limit)
    for m in messages:
        m["id"] = str(m.pop("_id"))
    return messages[::-1]

@app.post("/takeover/{user_id}", dependencies=[Depends(get_current_user)])
async def set_takeover(user_id: str, request: TakeoverRequest):
    await set_human_takeover_status(user_id, request.is_human)
    return {"status": "success", "is_human": request.is_human}

@app.get("/takeover/{user_id}", dependencies=[Depends(get_current_user)])
async def get_takeover(user_id: str):
    is_human = await get_human_takeover_status(user_id)
    return {"is_human": is_human}

@app.get("/faq", dependencies=[Depends(get_current_user)])
async def get_all_faqs():
    faqs = await get_faqs()
    for f in faqs:
        f["id"] = str(f.pop("_id"))
    return faqs

@app.post("/faq", dependencies=[Depends(get_current_user)])
async def create_faq(request: FAQRequest):
    await add_faq(request.question, request.answer)
    return {"status": "success"}

@app.put("/faq/{faq_id}", dependencies=[Depends(get_current_user)])
async def update_faq_endpoint(faq_id: str, request: FAQRequest):
    await update_faq(faq_id, request.question, request.answer)
    return {"status": "success"}

@app.delete("/faq/{faq_id}", dependencies=[Depends(get_current_user)])
async def delete_faq_endpoint(faq_id: str):
    await delete_faq(faq_id)
    return {"status": "success"}

@app.post("/upload", dependencies=[Depends(get_current_user)])
async def upload_document(file: UploadFile = File(...)):
    name = file.filename
    content = ""
    file_bytes = await file.read()
    
    # Support multiple file types via local extraction
    if name.endswith('.txt'):
        content = file_bytes.decode('utf-8')
    else:
        # For non-txt, we still store the reference for now
        # You can expand this with PyPDF2 or docx-python easily
        content = f"[Uploaded File: {name}]"
        
    await add_knowledge(name, content)
    return {"status": "success", "filename": name}

@app.get("/knowledge", dependencies=[Depends(get_current_user)])
async def get_knowledge():
    docs = await get_all_knowledge()
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return docs

@app.get("/config", dependencies=[Depends(get_current_user)])
async def get_config():
    config = await get_ai_config()
    if config and "_id" in config:
        config["_id"] = str(config["_id"])
    return config

@app.post("/config", dependencies=[Depends(get_current_user)])
async def set_config(request: ConfigRequest):
    await update_ai_config(request.engine, request.provider, request.fallback_enabled, request.system_prompt)
    
    # Force update memory
    ai_engine.preferred_provider = request.provider
    ai_engine.fallback_enabled = request.fallback_enabled
    if request.system_prompt:
        ai_engine.system_prompt = request.system_prompt
        print(f"🧠 System Identity Updated: {request.system_prompt[:50]}...")
        
    if "gemini" in request.engine.lower():
        ai_engine.gemini_model = request.engine
    else:
        ai_engine.openai_model = request.engine
    return {"status": "success"}

@app.get("/profile", dependencies=[Depends(get_current_user)])
async def get_profile(current_user: str = Depends(get_current_user)):
    return await get_admin_profile(current_user)

class ProfileRequest(BaseModel):
    name: str
    email: str
    avatar_url: Optional[str] = None

@app.post("/profile")
async def set_profile(request: ProfileRequest, current_user: str = Depends(get_current_user)):
    await update_admin_profile(current_user, request.name, request.email, request.avatar_url)
    return {"status": "success"}

from fastapi import UploadFile, File
import httpx

@app.post("/profile/avatar", dependencies=[Depends(get_current_user)])
async def upload_avatar(file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    token = os.getenv("BLOB_READ_WRITE_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="BLOB_READ_WRITE_TOKEN is missing in the backend .env file. Please add it to use image uploads.")
    
    file_bytes = await file.read()
    filename = f"avatar_{current_user}_{file.filename}"
    url = f"https://blob.vercel-storage.com/{filename}"
    
    headers = {
        "Authorization": f"Bearer {token}",
    }
    
    try:
        async with httpx.AsyncClient() as client:
            res = await client.put(url, headers=headers, content=file_bytes)
            res.raise_for_status()
            blob_data = res.json()
            avatar_url = blob_data["url"]
            
            # Update DB with new avatar
            profile = await get_admin_profile(current_user)
            await update_admin_profile(current_user, profile["name"], profile["email"], avatar_url=avatar_url)
            
            return {"status": "success", "avatar_url": avatar_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to upload to Vercel Blob: {str(e)}")

async def send_otp_email(to_email: str, otp: str):
    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_PASS")
    
    if not gmail_user or not gmail_pass:
        print(f"WARNING: Email credentials missing. OTP for {to_email} is {otp}")
        return False

    msg = MIMEMultipart()
    msg['From'] = gmail_user
    msg['To'] = to_email
    msg['Subject'] = "Pulse AI - Security Verification Code"

    body = f"""
    <h2>Security Verification Code</h2>
    <p>We received a request to change your Pulse AI Dashboard password.</p>
    <p>Your 6-digit verification code is:</p>
    <h1 style="color: #a855f7; font-size: 2.5rem; letter-spacing: 5px;">{otp}</h1>
    <p>This code will expire in 10 minutes. If you did not request this, please ignore this email.</p>
    <br/>
    <p>Best regards,<br/>Pulse AI Team</p>
    """
    msg.attach(MIMEText(body, 'html'))

    try:
        # Note: If this fails, user might need to use "App Password" for Gmail
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"Failed to send email: {e}")
        return False

@app.post("/auth/request-password-otp")
async def request_password_otp(current_user: str = Depends(get_current_user)):
    otp = ''.join(random.choices(string.digits, k=6))
    await save_otp(current_user, otp)
    
    email_sent = await send_otp_email(current_user, otp)
    
    log_msg = f"OTP sent to email: {current_user}" if email_sent else f"FAILED to send email. Code for dev: {otp}"
    print(f"\n{log_msg}\n")
    
    return {
        "status": "success" if email_sent else "warning", 
        "message": "Verification code sent to your email." if email_sent else "Email failed to send, but code is available in server logs (development mode)."
    }

class PasswordUpdateRequest(BaseModel):
    new_password: str
    otp: str

@app.post("/update-password")
async def update_password(request: PasswordUpdateRequest, current_user: str = Depends(get_current_user)):
    is_valid = await verify_otp(current_user, request.otp)
    if not is_valid:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP code.")
        
    await update_admin_password(current_user, request.new_password)
    return {"status": "success", "message": "Password updated successfully."}

class PreferencesRequest(BaseModel):
    notifications: bool
    auditLog: bool

@app.get("/preferences")
async def get_preferences(current_user: str = Depends(get_current_user)):
    prefs = await get_admin_preferences(current_user)
    if prefs and "_id" in prefs: 
        prefs["_id"] = str(prefs["_id"])
    return prefs

@app.post("/preferences")
async def set_preferences(request: PreferencesRequest, current_user: str = Depends(get_current_user)):
    await update_admin_preferences(current_user, request.notifications, request.auditLog)
    return {"status": "success"}

@app.get("/integrations", dependencies=[Depends(get_current_user)])
async def get_integrations():
    integrations = await get_integration_status()
    for item in integrations:
        if "_id" in item:
            item["_id"] = str(item["_id"])
    return integrations

@app.post("/integrations", dependencies=[Depends(get_current_user)])
async def set_integration(request: IntegrationRequest):
    await update_integration_status(request.platform, request.status)
    return {"status": "success"}

@app.post("/whatsapp/webhook")
async def whatsapp_webhook(From: str = Form(...), Body: str = Form(...), ProfileName: str = Form(default="WhatsApp User")):
    user_id = From.replace("whatsapp:", "")
    user_message = Body
    username = ProfileName or "WhatsApp User"

    is_human = await get_human_takeover_status(user_id)
    if is_human:
        await save_chat_history("whatsapp", user_id, user_message, "[HUMAN_TAKOVER_ACTIVE]", username=username)
        await manager.broadcast({
            "type": "new_message",
            "platform": "whatsapp",
            "user_id": user_id,
            "message": user_message,
            "response": "[HUMAN_TAKOVER_ACTIVE]",
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"status": "human_handling"}

    context = await get_user_context("whatsapp", user_id)
    faqs = await get_faqs()
    knowledge = await get_all_knowledge()
    
    response = await ai_engine.generate_response("whatsapp", user_id, user_message, context, faqs=faqs, knowledge=knowledge)
    whatsapp_bot.send_message(user_id, response)
    await save_chat_history("whatsapp", user_id, user_message, response, username=username)

    await notify_dashboard("whatsapp", user_id, user_message)

    return {"status": "success"}

class ManualResponseRequest(BaseModel):
    platform: str
    user_id: str
    message: str

import httpx

@app.post("/send-manual", dependencies=[Depends(get_current_user)])
async def send_manual(request: ManualResponseRequest):
    # 1. Force Human Takeover to ON
    await set_human_takeover_status(request.user_id, True)

    # 2. Send to Platform
    try:
        if request.platform == 'whatsapp':
            success = whatsapp_bot.send_message(request.user_id, request.message)
            if not success:
                return JSONResponse(status_code=500, content={"detail": "Twilio failed to deliver WhatsApp message. Check Twilio logs."})
        
        elif request.platform == 'telegram':
            token = os.getenv("TELEGRAM_TOKEN")
            async with httpx.AsyncClient() as client:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {"chat_id": request.user_id, "text": request.message}
                res = await client.post(url, json=payload)
                if res.status_code != 200:
                    return JSONResponse(status_code=res.status_code, content={"detail": f"Telegram API Error: {res.text}"})

        elif request.platform == 'discord':
            token = os.getenv("DISCORD_TOKEN")
            
            # Parse composite ID (user_id:channel_id) if it exists
            target_id = request.user_id
            if ":" in target_id:
                target_id = target_id.split(":")[1]
                
            async with httpx.AsyncClient() as client:
                url = f"https://discord.com/api/v10/channels/{target_id}/messages"
                headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
                payload = {"content": request.message}
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code != 200:
                    return JSONResponse(status_code=res.status_code, content={"detail": f"Discord API Error: {res.text}"})
        
        # 3. Save to History
        await save_chat_history(request.platform, request.user_id, f"[ADMIN]: {request.message}", "N/A")

        # 4. Broadcast via WebSocket
        await manager.broadcast({
            "type": "new_message",
            "platform": request.platform,
            "user_id": request.user_id,
            "message": f"[ADMIN]: {request.message}",
            "response": "N/A",
            "timestamp": datetime.utcnow().isoformat()
        })

        return {"status": "success"}

    except Exception as e:
        return JSONResponse(status_code=500, content={"detail": f"Critical Delivery Error: {str(e)}"})

@app.get("/history", response_model=List[ChatEntry], dependencies=[Depends(get_current_user)])
async def get_history(limit: int = 50):
    history_collection = db["chat_history"]
    cursor = history_collection.find().sort("timestamp", -1).limit(limit)
    history = await cursor.to_list(length=limit)
    for entry in history:
        entry["id"] = str(entry.pop("_id"))
    return history

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
