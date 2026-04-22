#!/bin/bash

echo "🚀 Starting Pulse AI Deployment..."

# 1. Update System
sudo apt update
sudo apt install -y python3-pip python3-venv nodejs npm

# 2. Install PM2 globally
sudo npm install -g pm2

# 3. Setup Python Virtual Environment
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# 4. Install Dependencies
echo "📥 Installing python dependencies..."
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 5. Start Services with PM2
echo "🔄 Starting services via PM2..."
pm2 start ecosystem.config.js
pm2 save

echo "✅ Deployment Complete!"
echo "Use 'pm2 status' to check your services."
echo "Use 'pm2 logs' to see live logs."
