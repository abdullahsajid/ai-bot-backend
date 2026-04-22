import os
from twilio.rest import Client
from dotenv import load_dotenv

load_dotenv()

class WhatsAppBot:
    def __init__(self):
        self.account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        self.auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        self.whatsapp_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
        
        if self.account_sid and self.auth_token and self.whatsapp_number:
            self.client = Client(self.account_sid, self.auth_token)
            print("Twilio WhatsApp client initialized.")
        else:
            self.client = None
            print("⚠️ Twilio credentials missing. WhatsApp bot running in MOCK MODE (messages will print to console).")

    def send_message(self, to_number, message_body):
        if not self.client:
            print(f"\n--- [WAPP MOCK] Sending to {to_number} ---")
            print(f"Message: {message_body}")
            print(f"----------------------------------------\n")
            return True
        
        try:
            # Twilio WhatsApp numbers must be prefixed with 'whatsapp:'
            from_whatsapp = f"whatsapp:{self.whatsapp_number}"
            to_whatsapp = f"whatsapp:{to_number}"
            
            # Twilio has a 1600 character limit for WhatsApp messages
            if len(message_body) > 1600:
                chunks = [message_body[i:i + 1500] for i in range(0, len(message_body), 1500)]
                for chunk in chunks:
                    self.client.messages.create(
                        body=chunk,
                        from_=from_whatsapp,
                        to=to_whatsapp
                    )
            else:
                self.client.messages.create(
                    body=message_body,
                    from_=from_whatsapp,
                    to=to_whatsapp
                )
            return True
        except Exception as e:
            print(f"Error sending WhatsApp message: {e}")
            return False

whatsapp_bot = WhatsAppBot()
