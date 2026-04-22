import urllib.parse
import urllib.request
import sys

def test_webhook(message="Hello, how are you?"):
    url = "http://localhost:8000/whatsapp/webhook"
    
    # Twilio sends data as Form URL Encoded
    # 'From' is usually "whatsapp:+123456789"
    # 'Body' is the message
    data = {
        "From": "whatsapp:+19998887776",
        "Body": message
    }
    
    encoded_data = urllib.parse.urlencode(data).encode("utf-8")
    
    print(f"Testing WhatsApp Webhook with message: '{message}'...")
    print(f"Sending request to {url}...")
    
    try:
        req = urllib.request.Request(url, data=encoded_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        with urllib.request.urlopen(req) as response:
            status = response.getcode()
            body = response.read().decode("utf-8")
            print(f"Status Code: {status}")
            print(f"Server Response: {body}")
            
            if status == 200:
                print("\n✅ Success! Check the terminal where you are running 'run_all.py'.")
                print("You should see the AI response printed there in Mock Mode.")
            else:
                print("\n❌ Failed with status code:", status)
                
    except Exception as e:
        print(f"\n❌ Error: Could not connect to the server. Make sure your backend is running!")
        print(f"Details: {e}")

if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "Hello from the test script!"
    test_webhook(msg)
