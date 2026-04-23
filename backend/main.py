import os
import uvicorn
from datetime import datetime, timedelta
from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect, Form, File, UploadFile, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel
from typing import List, Optional, Set
from contextlib import asynccontextmanager
import httpx
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

from .ai_engine import ai_engine
from .database import (
    db, save_chat_history, get_user_context, get_active_conversations, 
    get_human_takeover_status, set_human_takeover_status, get_faqs, 
    add_faq, update_faq, delete_faq, get_ai_config, update_ai_config, get_admin_profile, 
    update_admin_profile, get_integration_status, update_integration_status, 
    get_admin_user, update_admin_password, verify_password, 
    create_initial_admin, create_admin, get_admin_preferences, 
    update_admin_preferences, save_otp, verify_otp, add_knowledge, get_all_knowledge,
    get_user_thread, save_user_thread, add_notification, get_notifications, clear_notifications,
    is_account_locked, track_failed_login, reset_failed_login
)
from .bots.whatsapp import whatsapp_bot

import logging

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("server.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("PulseAI")

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
        if engine:
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

# WebSocket Manager
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

class SignupRequest(BaseModel):
    email: str
    password: str

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
    avatar_url: Optional[str] = None

class IntegrationRequest(BaseModel):
    platform: str
    status: str

class AppChatRequest(BaseModel):
    user_id: str
    message: str

class ManualResponseRequest(BaseModel):
    platform: str
    user_id: str
    message: str

class PasswordUpdateRequest(BaseModel):
    new_password: str
    otp: str

class PreferencesRequest(BaseModel):
    notifications: bool
    auditLog: bool

class Verify2FARequest(BaseModel):
    email: str
    otp: str
    recaptcha_token: Optional[str] = None

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

async def send_otp_email(to_email: str, otp: str):
    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_PASS")
    
    if not gmail_user or not gmail_pass:
        logger.warning(f"⚠️ [DEV MODE] OTP for {to_email} is: {otp}")
        return True

    msg = MIMEMultipart()
    msg['From'] = f"Pulse AI Security <{gmail_user}>"
    msg['To'] = to_email
    msg['Subject'] = "Your Pulse AI Verification Code"

    body = f"""
    <div style="font-family: sans-serif; padding: 20px; color: #333;">
        <h2 style="color: #a855f7;">Security Verification</h2>
        <p>A login attempt was made for your Pulse AI account.</p>
        <p>Your 6-digit verification code is:</p>
        <h1 style="background: #f3f4f6; padding: 10px; display: inline-block; letter-spacing: 5px;">{otp}</h1>
        <p>This code expires in 10 minutes. If you did not request this, please change your password immediately.</p>
    </div>
    """
    msg.attach(MIMEText(body, 'html'))
    try:
        server = smtplib.SMTP('smtp.gmail.com', 587, timeout=5)
        server.starttls()
        server.login(gmail_user, gmail_pass)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        # In case of network error, we don't crash, we just log the OTP for the admin
        logger.warning(f"⚠️ [FIREWALL ALERT] Port 587 might be blocked. OTP is: {otp}")
        return False

# --- Public Endpoints ---

@app.get("/")
async def root():
    return {"status": "Pulse AI API is running"}

@app.post("/api/chat")
async def app_chat_webhook(request: AppChatRequest):
    user_id = request.user_id
    user_message = request.message
    platform = "app"

    is_human = await get_human_takeover_status(user_id)
    if is_human:
        await save_chat_history(platform, user_id, user_message, "[HUMAN_TAKOVER_ACTIVE]")
        await manager.broadcast({
            "type": "new_message",
            "platform": platform,
            "user_id": user_id,
            "message": user_message,
            "response": "[HUMAN_TAKOVER_ACTIVE]",
            "timestamp": datetime.utcnow().isoformat()
        })
        return {"response": "A human agent will be with you shortly.", "status": "human_handling"}

    context = await get_user_context(platform, user_id)
    faqs = await get_faqs()
    knowledge = await get_all_knowledge()
    
    response = await ai_engine.generate_response(platform, user_id, user_message, context, faqs=faqs, knowledge=knowledge)

    await save_chat_history(platform, user_id, user_message, response)
    await manager.broadcast({
        "type": "new_message",
        "platform": platform,
        "user_id": user_id,
        "message": user_message,
        "response": response,
        "timestamp": datetime.utcnow().isoformat()
    })

    return {"response": response, "status": "success"}

# --- Auth Endpoints ---

@app.post("/auth/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    email = form_data.username
    password = form_data.password[:72] # Bcrypt limit
    
    # 1. Check for Account Lockout
    is_locked, locked_until = await is_account_locked(email)
    if is_locked:
        wait_mins = int((locked_until - datetime.utcnow()).total_seconds() / 60)
        raise HTTPException(status_code=403, detail=f"Account locked due to multiple failed attempts. Try again in {max(1, wait_mins)} minutes.")

    user = await get_admin_user(email) 
    
    # 2. Verify Password
    if not user or not verify_password(password, user["password"]):
        await track_failed_login(email)
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    # 3. Successful password, clear failures
    await reset_failed_login(email)
    
    # 4. [TEST MODE] Bypass 2FA and login immediately
    access_token = create_access_token(data={"sub": email})
    return {
        "access_token": access_token, 
        "token_type": "bearer", 
        "status": "success"
    }

@app.post("/auth/verify-2fa")
async def verify_login_2fa(request: Verify2FARequest):
    # Optional: Verify Recaptcha here
    if request.recaptcha_token:
        # verify_recaptcha(request.recaptcha_token)
        pass

    if await verify_otp(request.email, request.otp):
        access_token = create_access_token(data={"sub": request.email})
        return {"access_token": access_token, "token_type": "bearer", "status": "success"}
    
    raise HTTPException(status_code=400, detail="Invalid or expired verification code")

@app.post("/auth/signup")
async def signup(request: SignupRequest):
    success = await create_admin(request.email, request.password)
    if not success:
        raise HTTPException(status_code=400, detail="User already exists")
    return {"status": "success"}

# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# --- Internal Notify ---

@app.post("/internal/notify")
async def internal_notify(data: dict):
    platform = data.get("platform")
    user_id = data.get("user_id")
    message = data.get("message")
    response = data.get("response", "AI Processing...")
    
    # 1. Save to Database so it persists when dashboard is closed
    notif_text = f"New {platform} msg from {user_id}: {message[:30]}..."
    await add_notification(platform, user_id, notif_text)

    # 2. Broadcast to active dashboard viewers
    await manager.broadcast({
        "type": "new_message",
        "platform": platform,
        "user_id": user_id,
        "message": message,
        "response": response,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "ok"}

# --- Admin Stats & Dashboard ---

@app.get("/stats", dependencies=[Depends(get_current_user)])
async def get_stats(interval: str = "hourly"):
    history_collection = db["chat_history"]
    discord_count = await history_collection.count_documents({"platform": {"$regex": "^discord$", "$options": "i"}})
    telegram_count = await history_collection.count_documents({"platform": {"$regex": "^telegram$", "$options": "i"}})
    whatsapp_count = await history_collection.count_documents({"platform": {"$regex": "^whatsapp$", "$options": "i"}})
    
    total_messages = discord_count + telegram_count + whatsapp_count or await history_collection.count_documents({})

    now = datetime.utcnow()
    throughput = []
    if interval == "hourly":
        for i in range(23, -1, -1):
            start = now - timedelta(hours=i+1)
            end = now - timedelta(hours=i)
            count = await history_collection.count_documents({"timestamp": {"$gte": start, "$lt": end}})
            throughput.append({"label": start.strftime("%H:00"), "value": count})
    else:
        for i in range(6, -1, -1):
            start = (now - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            count = await history_collection.count_documents({"timestamp": {"$gte": start, "$lt": end}})
            throughput.append({"label": start.strftime("%b %d"), "value": count})

    active_users = await history_collection.distinct("user_id", {"timestamp": {"$gte": now - timedelta(hours=24)}})
    messages_today = await history_collection.count_documents({"timestamp": {"$gte": now.replace(hour=0, minute=0, second=0, microsecond=0)}})

    uptime_delta = datetime.utcnow() - START_TIME
    return {
        "total_messages": total_messages,
        "platform_stats": {"discord": discord_count, "telegram": telegram_count, "whatsapp": whatsapp_count},
        "throughput": throughput,
        "active_users_count": len(active_users),
        "messages_today": messages_today,
        "uptime": f"{uptime_delta.days}d {uptime_delta.seconds//3600}h {(uptime_delta.seconds//60)%60}m",
        "stability": "99.98%"
    }

@app.get("/conversations", dependencies=[Depends(get_current_user)])
async def get_conversations():
    convos = await get_active_conversations()
    for c in convos: c["user_id"] = c.pop("_id")
    return convos

@app.get("/messages/{platform}/{user_id}", dependencies=[Depends(get_current_user)])
async def get_messages(platform: str, user_id: str, limit: int = 50):
    messages = await db["chat_history"].find({"platform": platform, "user_id": str(user_id)}).sort("timestamp", -1).to_list(length=limit)
    for m in messages: m["id"] = str(m.pop("_id"))
    return messages[::-1]

@app.post("/takeover/{user_id}", dependencies=[Depends(get_current_user)])
async def set_takeover(user_id: str, request: TakeoverRequest):
    await set_human_takeover_status(user_id, request.is_human)
    return {"status": "success"}

@app.get("/takeover/{user_id}", dependencies=[Depends(get_current_user)])
async def get_takeover(user_id: str):
    return {"is_human": await get_human_takeover_status(user_id)}

@app.get("/faq", dependencies=[Depends(get_current_user)])
async def get_all_faqs():
    faqs = await get_faqs()
    for f in faqs: f["id"] = str(f.pop("_id"))
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
    content = (await file.read()).decode('utf-8') if file.filename.endswith('.txt') else f"[Binary: {file.filename}]"
    await add_knowledge(file.filename, content)
    return {"status": "success"}

@app.get("/knowledge", dependencies=[Depends(get_current_user)])
async def get_knowledge():
    docs = await get_all_knowledge()
    for d in docs: d["id"] = str(d.pop("_id"))
    return docs

class ScrapeRequest(BaseModel):
    url: str

@app.post("/scrape", dependencies=[Depends(get_current_user)])
async def scrape_website(request: ScrapeRequest):
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(request.url, timeout=15.0)
            if response.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Failed to reach website: {response.status_code}")
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # Clean up: remove script and style elements
            for script_or_style in soup(["script", "style"]):
                script_or_style.extract()
            
            # Get text
            text = soup.get_text(separator='\n')
            
            # Basic cleaning: break into lines and remove leading/trailing whitespace
            lines = (line.strip() for line in text.splitlines())
            # Break multi-headlines into a line each
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            # Drop blank lines
            clean_text = '\n'.join(chunk for chunk in chunks if chunk)
            
            title = soup.title.string if soup.title else request.url
            await add_knowledge(f"Scraped: {title}", clean_text)
            
            return {"status": "success", "title": title}
    except Exception as e:
        logger.error(f"Scraping failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/config", dependencies=[Depends(get_current_user)])
async def get_config_endpoint():
    config = await get_ai_config()
    if config and "_id" in config: config["_id"] = str(config["_id"])
    return config

@app.post("/config", dependencies=[Depends(get_current_user)])
async def set_config_endpoint(request: ConfigRequest):
    await update_ai_config(request.engine, request.provider, request.fallback_enabled, request.system_prompt)
    ai_engine.preferred_provider = request.provider
    ai_engine.fallback_enabled = request.fallback_enabled
    if request.system_prompt: ai_engine.system_prompt = request.system_prompt
    if "gemini" in request.engine.lower(): ai_engine.gemini_model = request.engine
    else: ai_engine.openai_model = request.engine
    return {"status": "success"}

@app.get("/profile", dependencies=[Depends(get_current_user)])
async def get_profile_endpoint(current_user: str = Depends(get_current_user)):
    return await get_admin_profile(current_user)

@app.post("/profile", dependencies=[Depends(get_current_user)])
async def set_profile_endpoint(request: ProfileRequest, current_user: str = Depends(get_current_user)):
    await update_admin_profile(current_user, request.name, request.email, request.avatar_url)
    return {"status": "success"}

@app.post("/profile/avatar", dependencies=[Depends(get_current_user)])
async def upload_avatar_endpoint(file: UploadFile = File(...), current_user: str = Depends(get_current_user)):
    token = os.getenv("BLOB_READ_WRITE_TOKEN")
    if not token: raise HTTPException(status_code=500, detail="BLOB_READ_WRITE_TOKEN missing")
    async with httpx.AsyncClient() as client:
        res = await client.put(f"https://blob.vercel-storage.com/avatar_{current_user}_{file.filename}", headers={"Authorization": f"Bearer {token}"}, content=await file.read())
        avatar_url = res.json()["url"]
        profile = await get_admin_profile(current_user)
        await update_admin_profile(current_user, profile["name"], profile["email"], avatar_url=avatar_url)
        return {"status": "success", "avatar_url": avatar_url}

@app.post("/auth/request-password-otp")
async def request_otp_endpoint(current_user: str = Depends(get_current_user)):
    otp = ''.join(random.choices(string.digits, k=6))
    await save_otp(current_user, otp)
    return {"status": "success", "message": "Code sent"}

@app.post("/update-password")
async def update_password_endpoint(request: PasswordUpdateRequest, current_user: str = Depends(get_current_user)):
    if await verify_otp(current_user, request.otp):
        await update_admin_password(current_user, request.new_password)
        return {"status": "success"}
    raise HTTPException(status_code=400, detail="Invalid OTP")

@app.get("/preferences")
async def get_prefs(current_user: str = Depends(get_current_user)):
    return await get_admin_preferences(current_user)

@app.post("/preferences")
async def set_prefs(request: PreferencesRequest, current_user: str = Depends(get_current_user)):
    await update_admin_preferences(current_user, request.notifications, request.auditLog)
    return {"status": "success"}

@app.get("/integrations", dependencies=[Depends(get_current_user)])
async def get_ints(): return await get_integration_status()

@app.post("/integrations", dependencies=[Depends(get_current_user)])
async def set_int(request: IntegrationRequest):
    await update_integration_status(request.platform, request.status)
    return {"status": "success"}

@app.post("/whatsapp/webhook")
async def whatsapp_webhook(From: str = Form(...), Body: str = Form(...), ProfileName: str = Form(default="WhatsApp User")):
    user_id = From.replace("whatsapp:", "")
    platform = "whatsapp"
    if await get_human_takeover_status(user_id):
        await save_chat_history(platform, user_id, Body, "[HUMAN_TAKOVER_ACTIVE]", username=ProfileName)
        await manager.broadcast({"type": "new_message", "platform": platform, "user_id": user_id, "message": Body, "response": "[HUMAN_TAKOVER_ACTIVE]"})
        return {"status": "human"}
    
    response = await ai_engine.generate_response(platform, user_id, Body, await get_user_context(platform, user_id), faqs=await get_faqs(), knowledge=await get_all_knowledge())
    whatsapp_bot.send_message(user_id, response)
    await save_chat_history(platform, user_id, Body, response, username=ProfileName)
    await manager.broadcast({"type": "new_message", "platform": platform, "user_id": user_id, "message": Body, "response": response})
    return {"status": "success"}

@app.post("/send-manual", dependencies=[Depends(get_current_user)])
async def send_manual(request: ManualResponseRequest):
    await set_human_takeover_status(request.user_id, True)
    
    try:
        # 1. Deliver to Platform
        if request.platform == 'whatsapp':
            success = whatsapp_bot.send_message(request.user_id, request.message)
            if not success:
                raise HTTPException(status_code=500, detail="Twilio failed to deliver WhatsApp message")
        
        elif request.platform == 'telegram':
            token = os.getenv("TELEGRAM_TOKEN")
            async with httpx.AsyncClient() as client:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {"chat_id": request.user_id, "text": request.message}
                res = await client.post(url, json=payload)
                if res.status_code != 200:
                    raise HTTPException(status_code=res.status_code, detail=f"Telegram API Error: {res.text}")

        elif request.platform == 'discord':
            token = os.getenv("DISCORD_TOKEN")
            target_id = request.user_id.split(":")[1] if ":" in request.user_id else request.user_id
                
            async with httpx.AsyncClient() as client:
                url = f"https://discord.com/api/v10/channels/{target_id}/messages"
                headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
                payload = {"content": request.message}
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code != 200:
                    raise HTTPException(status_code=res.status_code, detail=f"Discord API Error: {res.text}")

        # 2. Save to History
        await save_chat_history(request.platform, request.user_id, f"[ADMIN]: {request.message}", "N/A")

        # 3. Update Dashboard Live Chat
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
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/history", response_model=List[ChatEntry], dependencies=[Depends(get_current_user)])
async def get_history_endpoint(limit: int = 50):
    history = await db["chat_history"].find().sort("timestamp", -1).to_list(length=limit)
    for entry in history: entry["id"] = str(entry.pop("_id"))
    return history

@app.get("/notifications", dependencies=[Depends(get_current_user)])
async def get_notifications_endpoint():
    return await get_notifications()

@app.delete("/notifications", dependencies=[Depends(get_current_user)])
async def clear_notifications_endpoint():
    await clear_notifications()
    return {"status": "success"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
