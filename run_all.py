import subprocess
import time
import sys
import os

def start_backend():
    print("Starting Dashboard Backend...")
    return subprocess.Popen([sys.executable, "-m", "backend.main"], cwd=".")

def start_discord():
    print("Starting Discord Bot...")
    return subprocess.Popen([sys.executable, "-m", "backend.bots.discord_bot"], cwd=".")

def start_telegram():
    print("Starting Telegram Bot...")
    return subprocess.Popen([sys.executable, "-m", "backend.bots.telegram_bot"], cwd=".")

def start_frontend():
    print("Starting Dashboard Frontend...")
    # This assumes npm install was run
    return subprocess.Popen(["npm", "run", "dev"], cwd="dashboard", shell=True)

if __name__ == "__main__":
    processes = []
    try:
        # Check for requirements
        # subprocess.run(["pip", "install", "-r", "requirements.txt"])
        
        backend = start_backend()
        processes.append(backend)
        
        discord = start_discord()
        processes.append(discord)
        
        telegram = start_telegram()
        processes.append(telegram)
        
        print("\nAll systems initiated. Press Ctrl+C to stop all services.")
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        print("\nShutting down...")
        for p in processes:
            p.terminate()
        print("Done.")
