#!/bin/bash
cd /var/www/YourIpoBot || exit 1
git checkout .
git pull origin main
./venv/bin/pip install -r requirements.txt
sudo systemctl restart youripobot
echo "[DEPLOY OK] $(date)" >> /var/log/youripobot_deploy.log
