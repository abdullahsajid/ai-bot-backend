#!/bin/bash
echo "🔄 Updating Pulse AI..."

# 1. Pull latest code
git pull

# 2. Update dependencies (just in case)
source venv/bin/activate
pip install -r requirements.txt

# 3. Restart all services with PM2
pm2 restart ecosystem.config.js

echo "✅ Update complete and services restarted!"
pm2 status
