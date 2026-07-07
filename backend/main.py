import os
import uvicorn
from datetime import datetime, timedelta
import asyncio
import re
import pytz
from fastapi import FastAPI, HTTPException, Depends, status, WebSocket, WebSocketDisconnect, Form, File, UploadFile, Request, Header, Response
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
    is_account_locked, track_failed_login, reset_failed_login, get_all_staff, delete_admin, update_admin,
    update_conversation_status, update_conversation_owner, set_conversation_wait, set_customer_name, update_admin_status,
    suggest_kb_articles, get_all_macros, add_macro, delete_macro
)
from .bots.whatsapp import whatsapp_bot

import logging

def is_human_requested(message: str) -> bool:
    if not message:
        return False
    msg_lower = message.lower().strip()
    if "customer identification submitted" in msg_lower:
        return True
    direct_words = {
        "human", "agent", "support", "live chat", "representative", "staff", 
        "assistance", "helpdesk", "csr", "live support", "operator"
    }
    if msg_lower in direct_words:
        return True
        
    trigger_phrases = [
        "talk to human", "talk to agent", "talk to a human", "talk to an agent",
        "speak to human", "speak to agent", "speak to a human", "speak to an agent",
        "human support", "customer support", "customer service", "live support",
        "live agent", "contact support", "connect to agent", "connect to a live agent",
        "connect to a human", "representative", "contact agent", "chat with human",
        "chat with agent", "speak with agent", "speak with human", "human assistance",
        "human csr", "connect to csr", "talk to staff", "speak to staff"
    ]
    return any(phrase in msg_lower for phrase in trigger_phrases)

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

async def release_inactive_takeovers_30m():
    # Find all users with active human takeover
    cursor = db["users"].find({"is_human_taking_over": True})
    active_users = await cursor.to_list(length=1000)
    now = datetime.utcnow()
    
    for user in active_users:
        user_id = user["user_id"]
        platform = user.get("platform", "app")
        
        # Get last message timestamp
        last_msg = await db["chat_history"].find_one(
            {"platform": platform, "user_id": user_id},
            sort=[("timestamp", -1)]
        )
        
        if last_msg:
            last_ts = last_msg.get("timestamp")
            if last_ts and (now - last_ts).total_seconds() > 1800: # 30 minutes
                # Disable human takeover
                await set_human_takeover_status(user_id, False)
                # Save system message
                await save_chat_history(platform, user_id, "Live Chat session expired. PulseAI is back online.", "N/A", username="System")
                # Broadcast status update
                await manager.broadcast({
                    "type": "new_message",
                    "platform": platform,
                    "user_id": user_id,
                    "message": "Live Chat session expired. PulseAI is back online.",
                    "response": "N/A",
                    "timestamp": datetime.utcnow().isoformat(),
                    "username": "System"
                })

async def cron_worker():
    while True:
        try:
            await asyncio.sleep(60) # Run every 60 seconds
            from .database import close_inactive_tickets_48h
            # 1. Release inactive takeovers
            await release_inactive_takeovers_30m()
            # 2. Close inactive tickets after 48h
            await close_inactive_tickets_48h()
        except Exception as e:
            logger.error(f"Error in background cron worker: {e}")

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
                
    # Reset all admin statuses to offline on startup
    try:
        from .database import db
        await db["admins"].update_many({}, {"$set": {"status": "offline"}})
        print("Reset all admin statuses to offline on startup.")
    except Exception as e:
        print(f"Failed to reset admin statuses: {e}")
                
    # Start background cron worker
    asyncio.create_task(cron_worker())
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
        self.agent_connections: Dict[str, Set[WebSocket]] = {} # email -> set of websockets
        self.mobile_connections: Dict[str, Set[WebSocket]] = {} # user_id -> set of websockets

    async def connect(self, websocket: WebSocket, email: str = None):
        await websocket.accept()
        self.active_connections.add(websocket)
        if email:
            if email not in self.agent_connections:
                self.agent_connections[email] = set()
                # Broadcast that this agent is online
                await self.broadcast({
                    "type": "agent_status_update",
                    "email": email,
                    "status": "online",
                    "timestamp": datetime.utcnow().isoformat()
                })
                # Update their status in the database to online
                try:
                    from .database import update_admin_status
                    await update_admin_status(email, "online")
                except Exception:
                    pass
            self.agent_connections[email].add(websocket)

    def disconnect(self, websocket: WebSocket, email: str = None):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        if email and email in self.agent_connections:
            if websocket in self.agent_connections[email]:
                self.agent_connections[email].remove(websocket)
            if len(self.agent_connections[email]) == 0:
                del self.agent_connections[email]
                # Broadcast that this agent is offline
                asyncio.create_task(self.broadcast({
                    "type": "agent_status_update",
                    "email": email,
                    "status": "offline",
                    "timestamp": datetime.utcnow().isoformat()
                }))
                # Update their status in the database to offline
                try:
                    from .database import update_admin_status
                    asyncio.create_task(update_admin_status(email, "offline"))
                except Exception:
                    pass

    async def connect_mobile(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.mobile_connections:
            self.mobile_connections[user_id] = set()
        self.mobile_connections[user_id].add(websocket)

    def disconnect_mobile(self, websocket: WebSocket, user_id: str):
        if user_id in self.mobile_connections:
            if websocket in self.mobile_connections[user_id]:
                self.mobile_connections[user_id].remove(websocket)
            if len(self.mobile_connections[user_id]) == 0:
                del self.mobile_connections[user_id]

    async def send_to_mobile(self, user_id: str, message: dict):
        if user_id in self.mobile_connections:
            for connection in list(self.mobile_connections[user_id]):
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                pass
        
        # Forward message to corresponding mobile socket if active
        user_id = message.get("user_id")
        if user_id:
            await self.send_to_mobile(user_id, message)

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
    name: Optional[str] = "Staff Member"
    role: Optional[str] = "SUPPORT AGENT"
    permissions: Optional[List[str]] = ["chat", "knowledge"]
    avatar_url: Optional[str] = ""


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
    telegram_mention_only: Optional[bool] = False

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
    platform: Optional[str] = "app"
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    location: Optional[str] = None

class TicketCreateRequest(BaseModel):
    customer_name: str
    customer_email: str
    subject: str
    description: str
    category: str

class IncomingEmailRequest(BaseModel):
    sender_name: str
    sender_email: str
    subject: str
    body: str

class TicketReplyRequest(BaseModel):
    message: str

class TicketStatusRequest(BaseModel):
    status: str

class TicketAssignRequest(BaseModel):
    agent_email: str

class EscalateCTORequest(BaseModel):
    platform: str
    user_id: str
    target_email: str
    notes: Optional[str] = ""

class BanRequest(BaseModel):
    ip_address: Optional[str] = None
    email: Optional[str] = None
    reason: Optional[str] = ""

class OperatingHoursRequest(BaseModel):
    timezone: str
    schedule: dict

class MobileChatRequest(BaseModel):
    user_id: str
    message: str
    screen_context: Optional[str] = "main_wallet"
    platform: Optional[str] = "mobile"
    customer_name: Optional[str] = None
    customer_email: Optional[str] = None
    location: Optional[str] = None

class TransactionAnalysisRequest(BaseModel):
    user_id: str
    amount: float
    currency: str
    fee: float
    destination: str

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

async def require_permission(required_perm: str, email: str):
    """Check if user has a specific permission or is an admin"""
    user = await get_admin_user(email)
    if not user:
        raise HTTPException(status_code=401, detail="Access denied. User not found.")
    
    role = user.get("role", "SUPPORT AGENT")
    permissions = user.get("permissions", [])
    
    # Bypass for admins
    if role in ["SUPER ADMIN", "ADMIN"]:
        return True
    
    # Check granular permissions
    if "all" in permissions or required_perm in permissions:
        return True
        
    raise HTTPException(
        status_code=403, 
        detail=f"Security access restricted. Permission '{required_perm}' required."
    )

async def send_otp_email(to_email: str, otp: str):
    # Use Resend API (Bypasses DigitalOcean port blocks)
    api_key = "re_6tZW8cri_KhTzKiV5jbJP2p3oUa7Tei72"
    sender_email = "security@lumopulse.us" # Verified domain in Resend
    
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": f"Lumo Security <{sender_email}>",
        "to": [to_email],
        "subject": "Your Pulse AI Verification Code",
        "html": f"""
            <div style="font-family: sans-serif; padding: 20px; color: #333;">
                <h2 style="color: #a855f7;">Security Verification</h2>
                <p>Your 6-digit verification code is:</p>
                <h1 style="background: #f3f4f6; padding: 10px; display: inline-block; letter-spacing: 5px;">{otp}</h1>
                <p>This code expires in 10 minutes.</p>
            </div>
        """
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code in [200, 201]:
                return True
            else:
                logger.error(f"Resend Error: {res.text}")
                # Fallback: Still print to logs so you can log in if API fails
                logger.warning(f"⚠️ [API FAIL] OTP for {to_email}: {otp}")
                return False
    except Exception as e:
        logger.error(f"Failed to send email via Resend: {e}")
        return False

# --- Public Endpoints ---

@app.get("/")
async def root():
    return {"status": "Pulse AI API is running"}

from fastapi import Header

@app.post("/api/chat")
async def app_chat_webhook(request: AppChatRequest, x_app_secret: str = Header(None)):
    expected_secret = os.getenv("APP_SECRET", "LumoMobileApp_Secret_2026")
    if x_app_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Unauthorized access. Invalid App Secret.")

    user_id = request.user_id
    user_message = request.message
    platform = request.platform or "app"

    # Reset is_unread status on incoming message
    from .database import db
    await db["users"].update_one(
        {"platform": platform, "user_id": str(user_id)},
        {"$set": {"is_unread": True}},
        upsert=True
    )

    from .database import is_customer_banned
    # Check if user is banned
    if await is_customer_banned(email=user_id) or await is_customer_banned(ip_address=user_id):
        raise HTTPException(status_code=403, detail="You have been banned from using the live chat.")

    if request.customer_name or request.customer_email:
        update_fields = {}
        if request.customer_name:
            update_fields["customer_name"] = request.customer_name
            update_fields["username"] = request.customer_name
        if request.customer_email:
            update_fields["customer_email"] = request.customer_email
        if update_fields:
            await db["users"].update_one(
                {"platform": platform, "user_id": str(user_id)},
                {"$set": update_fields},
                upsert=True
            )

    if request.location:
        await db["users"].update_one(
            {"platform": platform, "user_id": str(user_id)},
            {"$set": {"location": request.location}},
            upsert=True
        )

    # Manage chat status and waiting timer
    user_status = "new"
    u = await db["users"].find_one({"platform": platform, "user_id": str(user_id)})
    if not u:
        u = await db["users"].find_one({"user_id": str(user_id)})
    if u:
        user_status = u.get("status", "new")
    if user_status == "resolved":
        await update_conversation_status(platform, user_id, "new")
        user_status = "new"
    if user_status == "new":
        if not u or not u.get("wait_since"):
            await set_conversation_wait(platform, user_id, datetime.utcnow())

    if is_human_requested(user_message):
        await set_human_takeover_status(user_id, True)
        await update_conversation_status(platform, user_id, "in_progress")
        await manager.broadcast({
            "type": "conversation_status_update",
            "platform": platform,
            "user_id": user_id,
            "status": "in_progress",
            "timestamp": datetime.utcnow().isoformat()
        })
        await manager.broadcast({
            "type": "takeover_status_update",
            "platform": platform,
            "user_id": user_id,
            "is_human": True,
            "timestamp": datetime.utcnow().isoformat()
        })
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
    
    # Check if AI is active
    from .database import is_platform_active
    if not await is_platform_active(platform):
        return {"response": "[AI_DISABLED_BY_ADMIN]", "status": "disabled"}
    
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

async def verify_turnstile(token: str):
    """Verify Cloudflare Turnstile token"""
    if not token:
        print("DEBUG: No captcha token provided")
        return False
        
    secret = os.getenv("TURNSTILE_SECRET", "0x4AAAAAADDP1lRl_8Fx_ovNRnCuNz2ET4Y")
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": secret, "response": token}
        )
        data = res.json()
        if not data.get("success"):
            print(f"DEBUG: Turnstile Failed. Response: {data}")
        return data.get("success", False)

# --- Auth Endpoints ---

@app.post("/auth/login")
async def login(
    username: str = Form(...),
    password: str = Form(...),
    captcha_token: str = Form(...)
):
    email = username
    password = password[:72] # Bcrypt limit
    
    # 1. Verify Cloudflare Captcha (or check for a trusted server-side bypass token)
    api_bypass_token = os.getenv("API_BYPASS_TOKEN", "PulseAdmin_ServerAccess_2026")
    is_trusted_server = captcha_token == api_bypass_token
    
    if not is_trusted_server and not await verify_turnstile(captcha_token):
        raise HTTPException(status_code=400, detail="Security check failed. Please try again.")

    # 2. Check for Account Lockout
    is_locked, locked_until = await is_account_locked(email)
    if is_locked:
        wait_mins = int((locked_until - datetime.utcnow()).total_seconds() / 60)
        raise HTTPException(status_code=403, detail=f"Account locked. Try again in {max(1, wait_mins)} minutes.")

    user = await get_admin_user(email) 
    if not user:
        print(f"DEBUG: Login failed. User not found: {email}")
    
    # 3. Verify Password
    if not user or not verify_password(password, user["password"]):
        await track_failed_login(email)
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    
    # 4. Success - Reset failures and start 2FA
    await reset_failed_login(email)
    
    # Generate and send OTP
    otp = ''.join(random.choices(string.digits, k=6))
    await save_otp(email, otp)
    
    sent = await send_otp_email(email, otp)
    if not sent:
        # Fallback for dev/blocked ports: log it
        logger.warning(f"OTP for {email}: {otp}")
    
    return {
        "status": "2fa_required",
        "message": "Verification code sent to your email."
    }

@app.post("/auth/verify-2fa")
async def verify_login_2fa(request: Verify2FARequest):
    if await verify_otp(request.email, request.otp):
        # Fetch user info to return to frontend
        user = await get_admin_user(request.email)
        access_token = create_access_token(data={"sub": request.email})
        return {
            "access_token": access_token, 
            "token_type": "bearer", 
            "status": "success",
            "user": {
                "name": user.get("name", "Staff Member"),
                "email": user.get("email"),
                "role": user.get("role", "SUPPORT AGENT"),
                "permissions": user.get("permissions", ["chat", "knowledge"])
            }
        }
    
    raise HTTPException(status_code=400, detail="Invalid or expired verification code")

@app.post("/auth/signup")
async def signup(request: SignupRequest):
    success = await create_admin(request.email, request.password, name=request.name, role=request.role, permissions=request.permissions, avatar_url=request.avatar_url)
    if not success:
        raise HTTPException(status_code=400, detail="User already exists")
    return {"status": "success"}

# --- Staff Management ---

@app.get("/staff", dependencies=[Depends(get_current_user)])
async def list_staff(email: str = Depends(get_current_user)):
    await require_permission("all", email)
    return await get_all_staff()

@app.post("/staff/add", dependencies=[Depends(get_current_user)])
async def add_staff(request: SignupRequest, email: str = Depends(get_current_user)):
    await require_permission("all", email)
    success = await create_admin(request.email, request.password, name=request.name, role=request.role, permissions=request.permissions, avatar_url=request.avatar_url)
    if not success:
        raise HTTPException(status_code=400, detail="User already exists")
    return {"status": "success"}

@app.post("/staff/update", dependencies=[Depends(get_current_user)])
async def update_staff_endpoint(request: SignupRequest, email: str = Depends(get_current_user)):
    await require_permission("all", email)
    # We use SignupRequest because it has the fields we need
    update_data = {
        "name": request.name,
        "role": request.role,
        "permissions": request.permissions,
        "avatar_url": request.avatar_url
    }
    if request.password and request.password != "••••••••":
        update_data["password"] = request.password
        
    success = await update_admin(request.email, update_data)

    if not success:
        raise HTTPException(status_code=400, detail="Failed to update staff member")
    
    await manager.broadcast({
        "type": "permission_update",
        "email": request.email,
        "permissions": request.permissions,
        "role": request.role
    })
    return {"status": "success"}

@app.delete("/staff/{target_email}", dependencies=[Depends(get_current_user)])
async def delete_staff_endpoint(target_email: str, email: str = Depends(get_current_user)):
    await require_permission("all", email)
    await delete_admin(target_email)
    return {"status": "success"}

# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    token = websocket.query_params.get("token")
    email = None
    if token:
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get("sub")
        except Exception:
            pass

    await manager.connect(websocket, email)
    try:
        while True: await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, email)

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
async def get_stats(interval: str = "hourly", email: str = Depends(get_current_user)):
    # Verify permission
    await require_permission("stats", email)
    
    history_collection = db["chat_history"]
    
    # Get total counts efficiently
    discord_count = await history_collection.count_documents({"platform": {"$regex": "^discord$", "$options": "i"}})
    telegram_count = await history_collection.count_documents({"platform": {"$regex": "^telegram$", "$options": "i"}})
    whatsapp_count = await history_collection.count_documents({"platform": {"$regex": "^whatsapp$", "$options": "i"}})
    total_messages = discord_count + telegram_count + whatsapp_count
    
    now = datetime.utcnow()
    throughput = []

    if interval == "hourly":
        # OPTIMIZED: Use aggregation to get all 24 hours in ONE query
        start_bound = now - timedelta(hours=24)
        pipeline = [
            {"$match": {"timestamp": {"$gte": start_bound}}},
            {"$project": {
                "hour": {"$hour": "$timestamp"}
            }},
            {"$group": {
                "_id": "$hour",
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id": 1}}
        ]
        results = await history_collection.aggregate(pipeline).to_list(length=24)
        counts_map = {r["_id"]: r["count"] for r in results}
        
        for i in range(23, -1, -1):
            target_time = now - timedelta(hours=i)
            h = target_time.hour
            throughput.append({
                "label": target_time.strftime("%H:00"), 
                "value": counts_map.get(h, 0)
            })
    else:
        # OPTIMIZED: Use aggregation for daily stats (Last 7 days)
        start_bound = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        pipeline = [
            {"$match": {"timestamp": {"$gte": start_bound}}},
            {"$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$timestamp"}}
            }},
            {"$group": {
                "_id": "$date",
                "count": {"$sum": 1}
            }},
            {"$sort": {"_id": 1}}
        ]
        results = await history_collection.aggregate(pipeline).to_list(length=7)
        counts_map = {r["_id"]: r["count"] for r in results}
        
        for i in range(6, -1, -1):
            target_date = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            label = (now - timedelta(days=i)).strftime("%b %d")
            throughput.append({
                "label": label, 
                "value": counts_map.get(target_date, 0)
            })

    active_users = await history_collection.distinct("user_id", {"timestamp": {"$gte": now - timedelta(hours=24)}})
    messages_today = await history_collection.count_documents({"timestamp": {"$gte": now.replace(hour=0, minute=0, second=0, microsecond=0)}})
    
    # Calculate AI Ratio (AI messages vs Total)
    ai_messages = await history_collection.count_documents({"response": {"$ne": "N/A"}})
    ai_ratio = f"{int((ai_messages / total_messages * 100))}%" if total_messages > 0 else "0%"

    uptime_delta = datetime.utcnow() - START_TIME
    return {
        "total_messages": total_messages,
        "platform_stats": {"discord": discord_count, "telegram": telegram_count, "whatsapp": whatsapp_count},
        "throughput": throughput,
        "active_users_count": len(active_users),
        "messages_today": messages_today,
        "ai_ratio": ai_ratio,
        "uptime": f"{uptime_delta.days}d {uptime_delta.seconds//3600}h {(uptime_delta.seconds//60)%60}m",
        "stability": "99.99%"
    }

@app.get("/conversations", dependencies=[Depends(get_current_user)])
async def get_conversations(limit: int = 20, skip: int = 0, platform: Optional[str] = None, status: Optional[str] = None, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    convos = await get_active_conversations(limit=limit, skip=skip, platform=platform, status=status)
    for c in convos:
        c["user_id"] = c.pop("_id")
        c["is_unread"] = c.get("is_unread", True)
    return convos

@app.get("/messages/{platform}/{user_id}", dependencies=[Depends(get_current_user)])
async def get_messages(platform: str, user_id: str, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    # Get the NEWEST 100 messages first
    messages = await db["chat_history"].find({"platform": platform, "user_id": user_id}).sort("timestamp", -1).to_list(length=100)
    for m in messages: m["id"] = str(m.pop("_id"))
    # Reverse them for correct UI display (Oldest to Newest)
    return messages[::-1]

@app.post("/takeover/{user_id}", dependencies=[Depends(get_current_user)])
async def set_takeover(user_id: str, request: TakeoverRequest):
    await set_human_takeover_status(user_id, request.is_human)
    return {"status": "success"}

@app.get("/takeover/{user_id}")
async def get_takeover(user_id: str):
    return {"is_human": await get_human_takeover_status(user_id)}

@app.patch("/conversations/{platform}/{user_id}/read")
async def mark_convo_as_read(platform: str, user_id: str, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    from .database import db
    
    # 1. Mark as read globally
    await db["users"].update_one(
        {"platform": platform, "user_id": str(user_id)},
        {"$set": {"is_unread": False}},
        upsert=True
    )
    
    # 2. First-Touch Auto-Assignment
    convo = await db["users"].find_one({"platform": platform, "user_id": str(user_id)})
    if convo and not convo.get("owner_email"):
        admin_profile = await db["admins"].find_one({"email": email.lower()})
        agent_name = admin_profile.get("name", "Support Agent") if admin_profile else "Support Agent"
        
        await db["users"].update_one(
            {"platform": platform, "user_id": str(user_id)},
            {"$set": {
                "owner_email": email.lower(),
                "owner_name": agent_name
            }}
        )
        
        await manager.broadcast({
            "type": "conversation_owner_update",
            "platform": platform,
            "user_id": user_id,
            "owner_email": email.lower(),
            "owner_name": agent_name,
            "timestamp": datetime.utcnow().isoformat()
        })
        
    return {"status": "success"}

# --- Conversation Upgrades Endpoints ---

class StatusPatchRequest(BaseModel):
    status: str

@app.patch("/conversations/{platform}/{user_id}/status")
async def update_convo_status(platform: str, user_id: str, request: StatusPatchRequest, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    if request.status not in ("new", "in_progress", "resolved", "banned"):
        raise HTTPException(status_code=400, detail="Invalid status. Must be new, in_progress, resolved, or banned.")
    
    await update_conversation_status(platform, user_id, request.status)
    if request.status == "resolved":
        await set_conversation_wait(platform, user_id, None)
        # Auto-release human takeover when resolved
        await set_human_takeover_status(user_id, False)
    elif request.status == "new":
        await set_conversation_wait(platform, user_id, datetime.utcnow())
        
    await manager.broadcast({
        "type": "conversation_status_update",
        "platform": platform,
        "user_id": user_id,
        "status": request.status,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "success"}

class OwnerPatchRequest(BaseModel):
    owner_email: Optional[str] = None

@app.patch("/conversations/{platform}/{user_id}/owner")
async def update_convo_owner(platform: str, user_id: str, request: OwnerPatchRequest, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    owner_name = None
    if request.owner_email:
        admin_profile = await get_admin_profile(request.owner_email)
        owner_name = admin_profile.get("name", "Staff Member")
        
    await update_conversation_owner(platform, user_id, request.owner_email, owner_name)
    await manager.broadcast({
        "type": "conversation_owner_update",
        "platform": platform,
        "user_id": user_id,
        "owner_email": request.owner_email,
        "owner_name": owner_name,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "success"}

class NamePatchRequest(BaseModel):
    customer_name: str

@app.patch("/conversations/{platform}/{user_id}/name")
async def update_convo_customer_name(platform: str, user_id: str, request: NamePatchRequest, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    await set_customer_name(platform, user_id, request.customer_name)
    await manager.broadcast({
        "type": "conversation_name_update",
        "platform": platform,
        "user_id": user_id,
        "customer_name": request.customer_name,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "success"}

@app.post("/takeover/{platform}/{user_id}")
async def set_takeover_platform(platform: str, user_id: str, request: TakeoverRequest, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    await set_human_takeover_status(user_id, request.is_human)
    
    admin_profile = await get_admin_profile(email)
    admin_name = admin_profile.get("name", "Staff Member")
    admin_avatar = admin_profile.get("avatar_url", "")
    
    if request.is_human:
        sys_msg = f"{admin_name} Joined the chat"
        await save_chat_history(platform, user_id, sys_msg, "N/A", username="System")
        await update_conversation_status(platform, user_id, "in_progress")
        await update_conversation_owner(platform, user_id, email, admin_name)
        await set_conversation_wait(platform, user_id, None)

        await manager.broadcast({
            "type": "agent_joined",
            "platform": platform,
            "user_id": user_id,
            "agent_name": admin_name,
            "agent_avatar": admin_avatar,
            "timestamp": datetime.utcnow().isoformat()
        })
    else:
        sys_msg = f"{admin_name} Left the chat"
        await save_chat_history(platform, user_id, sys_msg, "N/A", username="System")
        await manager.broadcast({
            "type": "agent_left",
            "platform": platform,
            "user_id": user_id,
            "agent_name": admin_name,
            "timestamp": datetime.utcnow().isoformat()
        })
    return {"status": "success", "is_human": request.is_human}

class AgentStatusRequest(BaseModel):
    status: str

@app.post("/agent/status")
async def change_agent_status(request: AgentStatusRequest, email: str = Depends(get_current_user)):
    if request.status not in ("online", "offline", "after_chat_work"):
        raise HTTPException(status_code=400, detail="Invalid status. Must be online, offline, or after_chat_work.")
        
    await update_admin_status(email, request.status)
    await manager.broadcast({
        "type": "agent_status_update",
        "email": email,
        "status": request.status,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "success"}

@app.get("/kb-suggest")
async def get_kb_suggestions(query: str = ""):
    from .database import suggest_kb_articles
    return await suggest_kb_articles(query)

@app.get("/macros", dependencies=[Depends(get_current_user)])
async def list_macros():
    return await get_all_macros()

class MacroCreateRequest(BaseModel):
    title: str
    content: str

@app.post("/macros")
async def create_macro(request: MacroCreateRequest, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    macro_id = await add_macro(request.title, request.content)
    return {"status": "success", "id": macro_id}

@app.delete("/macros/{macro_id}")
async def remove_macro(macro_id: str, email: str = Depends(get_current_user)):
    await require_permission("chat", email)
    await delete_macro(macro_id)
    return {"status": "success"}

@app.get("/faq", dependencies=[Depends(get_current_user)])
async def get_all_faqs():
    faqs = await get_faqs()
    for f in faqs: f["id"] = str(f.pop("_id"))
    return faqs

@app.post("/faq", dependencies=[Depends(get_current_user)])
async def add_faq_endpoint(request: FAQRequest, email: str = Depends(get_current_user)):
    await require_permission("knowledge", email)
    await add_faq(request.question, request.answer)
    return {"status": "success"}

@app.put("/faq/{faq_id}", dependencies=[Depends(get_current_user)])
async def update_faq_endpoint(faq_id: str, request: FAQRequest, email: str = Depends(get_current_user)):
    await require_permission("knowledge", email)
    await update_faq(faq_id, request.question, request.answer)
    return {"status": "success"}

@app.delete("/faq/{faq_id}", dependencies=[Depends(get_current_user)])
async def delete_faq_endpoint(faq_id: str, email: str = Depends(get_current_user)):
    await require_permission("knowledge", email)
    await delete_faq(faq_id)
    return {"status": "success"}

@app.post("/upload", dependencies=[Depends(get_current_user)])
async def upload_document(file: UploadFile = File(...), email: str = Depends(get_current_user)):
    await require_permission("knowledge", email)
    content = (await file.read()).decode('utf-8') if file.filename.endswith('.txt') else f"[Binary: {file.filename}]"
    await add_knowledge(file.filename, content)
    return {"status": "success"}

@app.get("/knowledge", dependencies=[Depends(get_current_user)])
async def get_knowledge(email: str = Depends(get_current_user)):
    await require_permission("knowledge", email)
    docs = await get_all_knowledge()
    for d in docs: d["id"] = str(d.pop("_id"))
    return docs

class ScrapeRequest(BaseModel):
    url: str

@app.post("/scrape", dependencies=[Depends(get_current_user)])
async def scrape_website(request: ScrapeRequest, email: str = Depends(get_current_user)):
    await require_permission("knowledge", email)
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
async def set_config_endpoint(request: ConfigRequest, email: str = Depends(get_current_user)):
    await require_permission("settings", email)
    await update_ai_config(request.engine, request.provider, request.fallback_enabled, request.system_prompt, telegram_mention_only=request.telegram_mention_only)
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
    
    # 1. Always Notify Dashboard & Save History (Safety first)
    is_human = await get_human_takeover_status(user_id)
    
    # Check if AI is active
    from .database import is_platform_active
    ai_active = await is_platform_active(platform)
    
    if is_human or not ai_active:
        # If human mode is on OR AI is disabled, we still want to see the message in Live Chat
        status_note = "[HUMAN_TAKOVER_ACTIVE]" if is_human else "[AI_DISABLED]"
        await save_chat_history(platform, user_id, Body, status_note, username=ProfileName)
        await manager.broadcast({
            "type": "new_message", 
            "platform": platform, 
            "user_id": user_id, 
            "message": Body, 
            "response": status_note,
            "username": ProfileName
        })
        return {"status": "manual_or_disabled"}
        
    # 2. AI Processing (If active and not human mode)
    response = await ai_engine.generate_response(platform, user_id, Body, await get_user_context(platform, user_id), faqs=await get_faqs(), knowledge=await get_all_knowledge())
    whatsapp_bot.send_message(user_id, response)
    
    await save_chat_history(platform, user_id, Body, response, username=ProfileName)
    await manager.broadcast({
        "type": "new_message", 
        "platform": platform, 
        "user_id": user_id, 
        "message": Body, 
        "response": response,
        "username": ProfileName
    })
    return {"status": "success"}

@app.post("/send-manual")
async def send_manual(request: ManualResponseRequest, email: str = Depends(get_current_user)):
    await set_human_takeover_status(request.user_id, True)
    admin_profile = await get_admin_profile(email)
    admin_name = admin_profile.get("name", "Staff Member")
    
    await update_conversation_status(request.platform, request.user_id, "in_progress")
    await update_conversation_owner(request.platform, request.user_id, email, admin_name)
    await set_conversation_wait(request.platform, request.user_id, None)
    
    try:
        # 1. Deliver to Platform (Skip if it's a private note)
        if request.message.startswith("[NOTE]:"):
            pass
        elif request.platform == 'whatsapp':
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
            
            # Fetch the latest message for this user to find which channel to reply to
            from .database import db
            latest_msg = await db["chat_history"].find_one(
                {"platform": "discord", "user_id": request.user_id},
                sort=[("timestamp", -1)]
            )
            channel_id = latest_msg.get("channel_id") if latest_msg else None
            
            if channel_id:
                target_id = channel_id
            else:
                # Fallback: Attempt to open a DM channel if no channel_id exists
                async with httpx.AsyncClient() as client:
                    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
                    payload = {"recipient_id": request.user_id}
                    res = await client.post("https://discord.com/api/v10/users/@me/channels", headers=headers, json=payload)
                    if res.status_code == 200:
                        target_id = res.json()["id"]
                    else:
                        target_id = request.user_id
                
            async with httpx.AsyncClient() as client:
                url = f"https://discord.com/api/v10/channels/{target_id}/messages"
                headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
                payload = {"content": request.message}
                res = await client.post(url, headers=headers, json=payload)
                if res.status_code != 200:
                    raise HTTPException(status_code=res.status_code, detail=f"Discord API Error: {res.text}")

        elif request.platform in ('app', 'website'):
            # For mobile app / website users, deliver via WebSocket broadcast
            # The client must listen to the WebSocket and render this as a staff message
            await manager.broadcast({
                "type": "staff_reply",
                "platform": request.platform,
                "user_id": request.user_id,
                "message": request.message,
                "sender_name": admin_profile.get("name", "Live Agent"),
                "sender_title": admin_profile.get("role", "Support Agent"),
                "sender_avatar": admin_profile.get("avatar_url", ""),
                "timestamp": datetime.utcnow().isoformat()
            })

        # 2. Save to History
        await save_chat_history(
            request.platform,
            request.user_id,
            f"[ADMIN]: {request.message}",
            "N/A",
            username=admin_profile.get("name", "Support Agent"),
            avatar_url=admin_profile.get("avatar_url", "")
        )

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

# --- Mobile App API (Lumo Wallet Integration) ---

async def verify_mobile_secret(x_app_secret: str = Header(None)):
    """Verifies that the request is coming from the authorized Lumo Mobile App."""
    expected_secret = os.getenv("APP_SECRET", "LumoMobileApp_Secret_2026")
    if x_app_secret != expected_secret:
        raise HTTPException(status_code=401, detail="Security Breach: Invalid Mobile App Secret.")
    return True

@app.websocket("/mobile/ws")
async def mobile_websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect_mobile(websocket, user_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect_mobile(websocket, user_id)

@app.get("/mobile/messages")
async def mobile_messages_endpoint(user_id: str, platform: str = "app", _ = Depends(verify_mobile_secret)):
    from .database import db
    messages = await db["chat_history"].find({"platform": platform, "user_id": user_id}).sort("timestamp", 1).to_list(length=100)
    result = []
    for msg in messages:
        timestamp = msg.get("timestamp")
        
        # 1. User/Admin/System message
        text = msg.get("message", "")
        if text and text != "N/A":
            sender = "user"
            sender_name = "Customer"
            if text.startswith("[ADMIN]:"):
                sender = "agent"
                sender_name = msg.get("username") or "Support Agent"
                text = text.replace("[ADMIN]:", "").strip()
            elif text.startswith("[STAFF]:"):
                sender = "agent"
                sender_name = msg.get("username") or "Support Agent"
                text = text.replace("[STAFF]:", "").strip()
            elif text.startswith("[SYSTEM]:"):
                sender = "system"
                sender_name = "System"
                text = text.replace("[SYSTEM]:", "").strip()
            
            result.append({
                "sender": sender,
                "message": text,
                "timestamp": timestamp.isoformat() if timestamp else None,
                "sender_name": sender_name
            })
            
        # 2. Chatbot response
        ai_resp = msg.get("response", "")
        if ai_resp == "[HUMAN_TAKOVER_ACTIVE]":
            ai_resp = "A human agent will be with you shortly."

        if ai_resp and ai_resp != "N/A" and ai_resp != "undefined":
            bot_time = timestamp + timedelta(seconds=1) if timestamp else None
            result.append({
                "sender": "bot",
                "message": ai_resp,
                "timestamp": bot_time.isoformat() if bot_time else None,
                "sender_name": "Lumo AI"
            })
    return result

@app.post("/mobile/chat")
async def mobile_chat_endpoint(request: MobileChatRequest, _ = Depends(verify_mobile_secret)):
    """Secure endpoint for the Flutter Lumo Wallet assistant."""
    user_id = request.user_id
    user_message = request.message
    platform = request.platform or "mobile"

    # Reset is_unread status on incoming message
    from .database import db
    await db["users"].update_one(
        {"platform": platform, "user_id": str(user_id)},
        {"$set": {"is_unread": True}},
        upsert=True
    )

    from .database import is_customer_banned
    # Check if user is banned
    if await is_customer_banned(email=user_id) or await is_customer_banned(ip_address=user_id):
        raise HTTPException(status_code=403, detail="You have been banned from using the live chat.")

    if request.customer_name or request.customer_email:
        update_fields = {}
        if request.customer_name:
            update_fields["customer_name"] = request.customer_name
            update_fields["username"] = request.customer_name
        if request.customer_email:
            update_fields["customer_email"] = request.customer_email
        if update_fields:
            await db["users"].update_one(
                {"platform": platform, "user_id": str(user_id)},
                {"$set": update_fields},
                upsert=True
            )

    if request.location:
        await db["users"].update_one(
            {"platform": platform, "user_id": str(user_id)},
            {"$set": {"location": request.location}},
            upsert=True
        )

    # Manage chat status and waiting timer
    user_status = "new"
    u = await db["users"].find_one({"platform": platform, "user_id": str(user_id)})
    if not u:
        u = await db["users"].find_one({"user_id": str(user_id)})
    if u:
        user_status = u.get("status", "new")
    if user_status == "resolved":
        await update_conversation_status(platform, user_id, "new")
        user_status = "new"
    if user_status == "new":
        if not u or not u.get("wait_since"):
            await set_conversation_wait(platform, user_id, datetime.utcnow())

    if is_human_requested(user_message):
        await set_human_takeover_status(user_id, True)
        await update_conversation_status(platform, user_id, "in_progress")
        await manager.broadcast({
            "type": "conversation_status_update",
            "platform": platform,
            "user_id": user_id,
            "status": "in_progress",
            "timestamp": datetime.utcnow().isoformat()
        })
        await manager.broadcast({
            "type": "takeover_status_update",
            "platform": platform,
            "user_id": user_id,
            "is_human": True,
            "timestamp": datetime.utcnow().isoformat()
        })
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

    # Check if AI is active
    from .database import is_platform_active
    if not await is_platform_active(platform):
        return {"response": "[AI_DISABLED_BY_ADMIN]", "status": "disabled"}

    history_context = await get_user_context(platform, user_id)
    faqs = await get_faqs()
    knowledge = await get_all_knowledge()
    
    # Custom mobile system prompt injection based on screen
    mobile_prompt = f"You are Pulse AI inside Lumo Wallet. Current User Screen: {request.screen_context}. Help the user manage their assets securely."
    
    response = await ai_engine.generate_response(
        platform, user_id, user_message, 
        context=history_context, faqs=faqs, knowledge=knowledge
    )
    
    await save_chat_history(platform, user_id, user_message, response)
    
    await manager.broadcast({
        "type": "new_message",
        "platform": platform,
        "user_id": user_id,
        "message": user_message,
        "response": response,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return {"response": response}

@app.post("/mobile/analyze-transaction")
async def analyze_transaction_endpoint(request: TransactionAnalysisRequest, _ = Depends(verify_mobile_secret)):
    """Analyzes a transaction before the user confirms it in the mobile app."""
    prompt = f"""
    Analyze this crypto transaction for safety and efficiency:
    - Amount: {request.amount} {request.currency}
    - Fee: {request.fee} {request.currency}
    - Destination: {request.destination}
    
    Provide a short, 1-sentence advice for the mobile user. 
    Warn them if the fee is too high (more than 10% of amount) or if it looks like a risky transfer.
    """
    
    advice = await ai_engine.generate_response("mobile", request.user_id, prompt)
    return {"advice": advice}

class OnboardingRequest(BaseModel):
    user_id: str
    step: str  # e.g., 'wallet_creation', 'recovery_phrase', 'first_transaction'
    question: Optional[str] = None

@app.post("/mobile/onboarding")
async def onboarding_endpoint(request: OnboardingRequest, _ = Depends(verify_mobile_secret)):
    """
    Guides new Lumo Wallet users through key onboarding steps.
    The Flutter app passes the current step and the AI provides contextual help.
    """
    step_context = {
        "wallet_creation": "The user is creating their Lumo Wallet for the first time. Guide them step-by-step, reassure them about security, and explain what they are setting up.",
        "recovery_phrase": "The user is viewing their 12/24 word recovery phrase. Emphasize its critical importance. Tell them to write it down offline, never share it, and that losing it means losing access to their funds forever.",
        "first_transaction": "The user is about to make their first transaction. Explain gas fees, how to double-check the destination address, and that blockchain transactions are irreversible.",
    }

    context_instruction = step_context.get(
        request.step,
        "You are Pulse AI onboarding assistant inside Lumo Wallet. Help the user get started."
    )

    user_q = request.question or f"Can you guide me through the '{request.step}' step?"
    full_prompt = f"{context_instruction}\n\nUser Question: {user_q}"

    response = await ai_engine.generate_response("mobile", request.user_id, full_prompt)
    return {"step": request.step, "guidance": response}

# --- Support Ticketing & Geolocation & Operating Hours APIs ---

async def send_customer_email(to_email: str, subject: str, html_content: str):
    api_key = os.getenv("RESEND_API_KEY", "re_gEMAYmWo_FHt74w9VnE4Q9CKC8ugr1FMM")
    sender_email = "support@lumowallet.com"
    
    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "from": f"Lumo Support <{sender_email}>",
        "to": [to_email],
        "reply_to": "support@lumowallet.com",
        "subject": subject,
        "html": html_content
    }

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(url, headers=headers, json=payload, timeout=10)
            if res.status_code in [200, 201]:
                return True
            else:
                logger.error(f"Resend Email Error: {res.text}")
                return False
    except Exception as e:
        logger.error(f"Failed to send email via Resend: {e}")
        return False


@app.post("/api/tickets/create")
async def api_create_ticket(request: TicketCreateRequest):
    from .database import create_ticket
    ticket = await create_ticket(
        request.customer_name,
        request.customer_email,
        request.subject,
        request.description,
        request.category
    )
    
    confirm_html = f"""
    <div style="font-family: sans-serif; padding: 20px; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e9ecef; border-radius: 12px;">
        <h2 style="color: #a855f7;">Lumo Support Ticket Confirmation</h2>
        <p>Dear {request.customer_name},</p>
        <p>We have received your support ticket request and our team is reviewing it.</p>
        <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <strong>Ticket Reference:</strong> {ticket['ticket_ref']}<br/>
            <strong>Subject:</strong> {request.subject}<br/>
            <strong>Status:</strong> Open
        </div>
        <p>You can reply directly to this email to add more details or follow up.</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
        <p style="font-size: 0.85rem; color: #6c757d;">
            One Wallet. Endless Possibilities. 💜<br/>
            Lumo Wallet Support Team
        </p>
    </div>
    """
    await send_customer_email(request.customer_email, f"Ticket Received: {request.subject} [{ticket['ticket_ref']}]", confirm_html)
    return {"status": "success", "ticket_ref": ticket["ticket_ref"]}

@app.post("/api/tickets/incoming")
async def api_incoming_email(request: IncomingEmailRequest):
    from .database import create_ticket, get_ticket, add_ticket_reply, update_ticket_status
    
    match = re.search(r"LUMO-\d{6,8}", request.subject)
    if match:
        ticket_ref = match.group(0)
        ticket = await get_ticket(ticket_ref)
        if ticket:
            await add_ticket_reply(
                ticket_ref=ticket_ref,
                sender_type="customer",
                sender_name=request.sender_name,
                message=request.body
            )
            if ticket.get("status") == "resolved":
                await update_ticket_status(ticket_ref, "open")
                
            await manager.broadcast({
                "type": "ticket_update",
                "ticket_ref": ticket_ref,
                "timestamp": datetime.utcnow().isoformat()
            })
            return {"status": "reply_added", "ticket_ref": ticket_ref}
            
    ticket = await create_ticket(
        customer_name=request.sender_name,
        customer_email=request.sender_email,
        subject=request.subject,
        description=request.body,
        category="email"
    )
    
    confirm_html = f"""
    <div style="font-family: sans-serif; padding: 20px; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e9ecef; border-radius: 12px;">
        <h2 style="color: #a855f7;">Lumo Support Ticket Created</h2>
        <p>Dear {request.sender_name},</p>
        <p>A support ticket has been created from your email.</p>
        <div style="background: #f8f9fa; padding: 15px; border-radius: 8px; margin: 20px 0;">
            <strong>Ticket Reference:</strong> {ticket['ticket_ref']}<br/>
            <strong>Subject:</strong> {request.subject}<br/>
            <strong>Status:</strong> Open
        </div>
        <p>You can reply directly to this email to follow up.</p>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;"/>
        <p style="font-size: 0.85rem; color: #6c757d;">
            One Wallet. Endless Possibilities. 💜<br/>
            Lumo Wallet Support Team
        </p>
    </div>
    """
    await send_customer_email(request.sender_email, f"Ticket Created: {request.subject} [{ticket['ticket_ref']}]", confirm_html)
    
    await manager.broadcast({
        "type": "new_ticket",
        "ticket_ref": ticket["ticket_ref"],
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "ticket_created", "ticket_ref": ticket["ticket_ref"]}

@app.get("/api/tickets", dependencies=[Depends(get_current_user)])
async def api_list_tickets(response: Response, status: Optional[str] = None, agent_email: Optional[str] = None, limit: int = 50, skip: int = 0, search: Optional[str] = None, email: str = Depends(get_current_user)):
    from .database import db
    query = {}
    if status:
        query["status"] = status
    if agent_email:
        query["assigned_agent_email"] = agent_email
    if search:
        search_val = search.strip()
        query["$or"] = [
            {"ticket_ref": {"$regex": search_val, "$options": "i"}},
            {"subject": {"$regex": search_val, "$options": "i"}},
            {"customer_name": {"$regex": search_val, "$options": "i"}},
            {"customer_email": {"$regex": search_val, "$options": "i"}}
        ]
        
    total_count = await db["tickets"].count_documents(query)
    response.headers["X-Total-Count"] = str(total_count)
    
    cursor = db["tickets"].find(query).sort("updated_at", -1).skip(skip).limit(limit)
    tickets = await cursor.to_list(length=limit)
    for t in tickets:
        t["_id"] = str(t["_id"])
        t["is_unread"] = t.get("is_unread", True)
    return tickets

@app.get("/api/tickets/{ticket_ref}", dependencies=[Depends(get_current_user)])
async def api_get_ticket_details(ticket_ref: str, email: str = Depends(get_current_user)):
    from .database import get_ticket, db
    ticket = await get_ticket(ticket_ref)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    
    await db["tickets"].update_one(
        {"ticket_ref": ticket_ref},
        {"$set": {"is_unread": False}}
    )
    
    ticket["is_unread"] = False
    return ticket

@app.post("/api/tickets/{ticket_ref}/reply", dependencies=[Depends(get_current_user)])
async def api_reply_ticket(ticket_ref: str, request: TicketReplyRequest, email: str = Depends(get_current_user)):
    from .database import get_ticket, add_ticket_reply, get_admin_profile
    
    ticket = await get_ticket(ticket_ref)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
        
    profile = await get_admin_profile(email)
    agent_name = profile.get("name", "Support Agent")
    agent_role = profile.get("role", "SUPPORT AGENT")
    agent_avatar = profile.get("avatar_url", "")
    
    success = await add_ticket_reply(
        ticket_ref=ticket_ref,
        sender_type="agent",
        sender_name=agent_name,
        message=request.message,
        sender_title=agent_role,
        sender_avatar=agent_avatar
    )
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to add reply")
        
    message_html = request.message.replace('\n', '<br/>')
    avatar_img_tag = f'<img src="{agent_avatar}" style="width: 48px; height: 48px; border-radius: 50%; object-fit: cover; display: block; margin-bottom: 8px;" />' if agent_avatar else ''
    
    email_html = f"""
    <div style="font-family: sans-serif; padding: 20px; color: #333; max-width: 600px; margin: 0 auto; border: 1px solid #e9ecef; border-radius: 12px;">
        <div style="margin-bottom: 20px;">
            <strong>Ticket Ref:</strong> {ticket_ref}<br/>
            <strong>Subject:</strong> {ticket['subject']}
        </div>
        <div style="background: #fdfbf7; border-left: 4px solid #a855f7; padding: 15px; margin-bottom: 25px; font-size: 1rem; line-height: 1.5; color: #1a1a1a;">
            {message_html}
        </div>
        <div style="display: flex; align-items: center; gap: 12px; margin-top: 20px;">
            {avatar_img_tag}
            <div>
                <strong style="display: block; font-size: 0.95rem; color: #111;">{agent_name}</strong>
                <span style="font-size: 0.8rem; color: #666; display: block;">{agent_role}</span>
                <span style="font-size: 0.8rem; color: #a855f7; font-weight: bold; display: block; margin-top: 4px;">Lumo Wallet Support</span>
            </div>
        </div>
        <hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;"/>
        <p style="font-size: 0.8rem; color: #999; text-align: center; margin: 0;">
            One Wallet. Endless Possibilities. 💜
        </p>
    </div>
    """
    
    await send_customer_email(
        ticket["customer_email"],
        f"Re: {ticket['subject']} [{ticket_ref}]",
        email_html
    )
    
    await manager.broadcast({
        "type": "ticket_update",
        "ticket_ref": ticket_ref,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    return {"status": "success"}

@app.patch("/api/tickets/{ticket_ref}/status", dependencies=[Depends(get_current_user)])
async def api_patch_ticket_status(ticket_ref: str, request: TicketStatusRequest, email: str = Depends(get_current_user)):
    from .database import update_ticket_status
    if request.status not in ("open", "escalated", "resolved", "spam"):
        raise HTTPException(status_code=400, detail="Invalid status")
    await update_ticket_status(ticket_ref, request.status)
    
    await manager.broadcast({
        "type": "ticket_update",
        "ticket_ref": ticket_ref,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "success"}

@app.patch("/api/tickets/{ticket_ref}/assign", dependencies=[Depends(get_current_user)])
async def api_patch_ticket_assign(ticket_ref: str, request: TicketAssignRequest, email: str = Depends(get_current_user)):
    from .database import assign_ticket
    await assign_ticket(ticket_ref, request.agent_email)
    
    await manager.broadcast({
        "type": "ticket_update",
        "ticket_ref": ticket_ref,
        "timestamp": datetime.utcnow().isoformat()
    })
    return {"status": "success"}

@app.post("/api/chat/escalate-to-cto", dependencies=[Depends(get_current_user)])
async def api_escalate_to_cto(request: EscalateCTORequest, email: str = Depends(get_current_user)):
    from .database import get_admin_profile
    
    messages = await db["chat_history"].find({"platform": request.platform, "user_id": request.user_id}).sort("timestamp", -1).to_list(length=100)
    messages = messages[::-1]
    
    profile = await get_admin_profile(email)
    agent_name = profile.get("name", "Support Agent")
    
    transcript_html = ""
    for m in messages:
        sender = m.get("username", "Customer")
        msg = m.get("message", "")
        resp = m.get("response", "")
        ts = m.get("timestamp", datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S")
        
        transcript_html += f"""
        <div style="margin-bottom: 15px; border-bottom: 1px solid #f1f3f5; padding-bottom: 8px;">
            <span style="font-size: 0.8rem; color: #888;">{ts} - <strong>{sender}</strong>:</span>
            <p style="margin: 4px 0; font-size: 0.95rem;">{msg}</p>
        </div>
        """
        if resp and resp != "N/A" and resp != "[HUMAN_TAKOVER_ACTIVE]":
            transcript_html += f"""
            <div style="margin-left: 20px; border-left: 2px solid #a855f7; padding-left: 10px; color: #555; margin-bottom: 15px;">
                <span style="font-size: 0.8rem; color: #888;">AI Response:</span>
                <p style="margin: 4px 0; font-size: 0.95rem;">{resp}</p>
            </div>
            """
        
    email_html = f"""
    <div style="font-family: sans-serif; padding: 20px; color: #333; max-width: 700px; margin: 0 auto; border: 1px solid #e9ecef; border-radius: 12px;">
        <h2 style="color: #ef4444; border-bottom: 2px solid #ef4444; padding-bottom: 10px;">🚨 Technical Escalation Request</h2>
        <p><strong>Escalated By:</strong> {agent_name} ({email})</p>
        <p><strong>Customer User ID:</strong> {request.user_id} ({request.platform})</p>
        <p><strong>Agent Notes:</strong> {request.notes}</p>
        
        <h3 style="margin-top: 30px; color: #111;">Chat History Transcript</h3>
        <div style="background: #f8f9fa; border: 1px solid #dee2e6; border-radius: 8px; padding: 15px; max-height: 400px; overflow-y: auto;">
            {transcript_html}
        </div>
        
        <hr style="border: 0; border-top: 1px solid #eee; margin: 25px 0;"/>
        <p style="font-size: 0.8rem; color: #999; text-align: center; margin: 0;">
            Lumo Wallet PulseAI Platform
        </p>
    </div>
    """
    
    sent = await send_customer_email(
        to_email=request.target_email,
        subject=f"🚨 [ESCALATION] Technical Issue - User {request.user_id}",
        html_content=email_html
    )
    
    if not sent:
        raise HTTPException(status_code=500, detail="Failed to send escalation email")
        
    sys_msg = f"Escalated to CTO ({request.target_email})"
    await save_chat_history(request.platform, request.user_id, f"[SYSTEM]: {sys_msg}", "N/A", username="System")
    
    await manager.broadcast({
        "type": "new_message",
        "platform": request.platform,
        "user_id": request.user_id,
        "message": f"[SYSTEM]: {sys_msg}",
        "response": "N/A",
        "timestamp": datetime.utcnow().isoformat(),
        "username": "System"
    })
    
    return {"status": "success"}

@app.get("/api/chat/banner-stats")
async def get_chat_banner_stats():
    online_agents = await db["admins"].find({"status": "online"}, {"password": 0}).to_list(length=100)
    for agent in online_agents:
        agent["_id"] = str(agent["_id"])
        
    queue_count = await db["users"].count_documents({"status": "new"})
    
    waiting_users = await db["users"].find({"status": "new", "wait_since": {"$ne": None}}).to_list(length=100)
    
    total_wait_seconds = 0
    count = 0
    now = datetime.utcnow()
    for u in waiting_users:
        wait_since = u.get("wait_since")
        if wait_since:
            total_wait_seconds += (now - wait_since).total_seconds()
            count += 1
            
    avg_wait_minutes = 2
    if count > 0:
        avg_wait_minutes = max(1, int((total_wait_seconds / count) / 60))
        
    return {
        "online_agents": online_agents,
        "avg_wait_time": f"{avg_wait_minutes} min",
        "queue_length": queue_count
    }

@app.get("/api/chat/operating-hours")
async def get_operating_hours_status():
    from .database import get_operating_hours
    config = await get_operating_hours()
    
    timezone_str = config.get("timezone", "UTC")
    schedule = config.get("schedule", {})
    
    try:
        tz = pytz.timezone(timezone_str)
    except Exception:
        tz = pytz.utc
        
    now_local = datetime.now(tz)
    weekday = now_local.strftime("%A").lower()
    
    day_config = schedule.get(weekday, {"enabled": False, "start": "00:00", "end": "00:00"})
    is_open = False
    
    if day_config.get("enabled"):
        start_str = day_config.get("start", "09:00")
        end_str = day_config.get("end", "17:00")
        
        try:
            start_time = datetime.strptime(start_str, "%H:%M").time()
            end_time = datetime.strptime(end_str, "%H:%M").time()
            current_time = now_local.time()
            
            if start_time <= current_time <= end_time:
                is_open = True
        except Exception:
            pass
            
    return {
        "is_open": is_open,
        "timezone": timezone_str,
        "schedule": schedule
    }

@app.post("/api/chat/operating-hours", dependencies=[Depends(get_current_user)])
async def api_update_operating_hours(request: OperatingHoursRequest, email: str = Depends(get_current_user)):
    await require_permission("settings", email)
    from .database import update_operating_hours
    await update_operating_hours(request.schedule, request.timezone)
    return {"status": "success"}

@app.post("/api/chat/ban", dependencies=[Depends(get_current_user)])
async def api_ban_customer(request: BanRequest):
    from .database import ban_customer
    await ban_customer(request.ip_address, request.email, request.reason)
    return {"status": "success"}

@app.post("/api/chat/unban", dependencies=[Depends(get_current_user)])
async def api_unban_customer(request: BanRequest):
    from .database import unban_customer
    await unban_customer(request.ip_address, request.email)
    return {"status": "success"}

@app.get("/api/chat/bans", dependencies=[Depends(get_current_user)])
async def api_get_bans():
    from .database import get_banned_customers
    return await get_banned_customers()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)

