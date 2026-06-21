import imaplib
import email
from email.header import decode_header
import re
import httpx
import asyncio
import logging

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("imap_poll")

# Configuration
IMAP_SERVER = "mail.lumowallet.com"
IMAP_PORT = 993
EMAIL_USER = "support@lumowallet.com"
EMAIL_PASS = "qR}~8E69vd0J^A~7" 

API_URL = "http://localhost:8000/api/tickets/incoming"

def clean_email_body(body):
    """Clean out quoted original messages from email threads"""
    # Normalize line endings
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    lines = body.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        
        # Break on reply headers: "On ... wrote:" or "From: ..." or "Sent: ..." or "---Original Message---"
        if (re.match(r"^On\s+.*wrote:\s*$", stripped, re.IGNORECASE) or
            re.match(r"^On\s+.*,\s+.*wrote:\s*$", stripped, re.IGNORECASE) or
            re.match(r"^-+Original Message-+$", stripped, re.IGNORECASE) or
            re.match(r"^From:\s*.*", stripped, re.IGNORECASE) or
            re.match(r"^Sent:\s*.*", stripped, re.IGNORECASE) or
            re.match(r"^To:\s*.*", stripped, re.IGNORECASE) or
            stripped == "---"):
            break
        cleaned.append(line)
        
    return "\n".join(cleaned).strip()

def extract_body(msg):
    """Extract plain text body from raw email message object"""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    payload = part.get_payload(decode=True)
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                    break
                except Exception:
                    pass
        if not body:  # Fallback to HTML if plain text not found
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/html":
                    try:
                        payload = part.get_payload(decode=True)
                        html = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
                        # Basic tag stripping
                        body = re.sub('<[^<]+?>', '', html)
                        break
                    except Exception:
                        pass
    else:
        try:
            payload = msg.get_payload(decode=True)
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="ignore")
        except Exception:
            pass
            
    return clean_email_body(body)

async def process_emails():
    logger.info("Connecting to mail server via IMAP...")
    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(EMAIL_USER, EMAIL_PASS)
        mail.select("inbox")
        
        # Search for all unread (UNSEEN) emails
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            logger.error("Failed to search inbox.")
            return

        email_ids = messages[0].split()
        if not email_ids:
            logger.info("No new emails found.")
            mail.logout()
            return
            
        logger.info(f"Found {len(email_ids)} unread email(s). Processing...")
        
        async with httpx.AsyncClient() as client:
            for e_id in email_ids:
                # Fetch raw email content
                res_status, msg_data = mail.fetch(e_id, "(RFC822)")
                if res_status != "OK":
                    continue
                    
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                # Parse Subject
                subject_raw = msg.get("Subject", "No Subject")
                subject, encoding = decode_header(subject_raw)[0]
                if isinstance(subject, bytes):
                    subject = subject.decode(encoding or "utf-8", errors="ignore")
                
                # Parse From
                from_raw = msg.get("From", "")
                sender_name = "Customer"
                sender_email = ""
                
                match = re.match(r"^(.*?)\s*<(.*?)>", from_raw)
                if match:
                    sender_name = match.group(1).strip(" \"'")
                    sender_email = match.group(2).strip()
                else:
                    sender_email = from_raw.strip()
                    sender_name = sender_email.split("@")[0]
                    
                # Extract text body
                body_text = extract_body(msg)
                if not body_text:
                    body_text = "Empty message body."
                    
                logger.info(f"Processing email from {sender_email} with subject: {subject}")
                
                # Post to local FastAPI endpoint
                payload = {
                    "sender_name": sender_name,
                    "sender_email": sender_email,
                    "subject": subject,
                    "body": body_text
                }
                
                try:
                    response = await client.post(API_URL, json=payload, timeout=15)
                    if response.status_code == 200:
                        logger.info("Successfully processed and posted to ticket system.")
                        # Mark email as read/seen on cPanel server
                        mail.store(e_id, "+FLAGS", "\\Seen")
                    else:
                        logger.error(f"Failed to post to API: Code {response.status_code} - {response.text}")
                except Exception as api_err:
                    logger.error(f"API Post Error: {api_err}")
                    
        mail.close()
        mail.logout()
    except Exception as e:
        logger.error(f"IMAP Connection Error: {e}")

if __name__ == "__main__":
    asyncio.run(process_emails())
