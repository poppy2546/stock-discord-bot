#!/usr/bin/env bash
# ==============================================================================
# Script ติดตั้งและรัน Stock Advisor Discord Bot บน Oracle Cloud (Ubuntu)
# ==============================================================================

set -e

echo "🚀 กำลังเริ่มต้นติดตั้ง Stock Advisor Discord Bot บนเซิร์ฟเวอร์..."

# 1. ติดตั้ง System Packages ที่จำเป็น
echo "📦 1/4 อัปเดตและติดตั้ง Python3, Pip, Virtualenv..."
sudo apt update -y
sudo apt install -y python3 python3-pip python3-venv git curl

# 2. สร้าง Virtual Environment
CURRENT_DIR=$(pwd)
echo "🐍 2/4 สร้าง Python Virtual Environment ที่: $CURRENT_DIR/venv"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# 3. ติดตั้ง Dependencies
echo "📥 3/4 กำลังติดตั้ง Dependencies จาก requirements.txt..."
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

# 4. สร้าง Systemd Service เพื่อรันบอทตลอด 24 ชั่วโมง
SERVICE_FILE="/etc/systemd/system/stockbot.service"
CURRENT_USER=$(whoami)

echo "⚙️ 4/4 สร้าง Systemd Service: $SERVICE_FILE"
sudo bash -c "cat << 'EOF' > $SERVICE_FILE
[Unit]
Description=Stock Advisor Discord Bot
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$CURRENT_DIR
ExecStart=$CURRENT_DIR/venv/bin/python3 discord_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

# 5. สั่งเริ่มระบบ
echo "🔄 รีโหลดและเปิดใช้งาน Service..."
sudo systemctl daemon-reload
sudo systemctl enable stockbot
sudo systemctl restart stockbot

echo "=========================================================="
echo "✅ ติดตั้งสำเร็จเรียบร้อย! บอทกำลังทำงานในเบื้องหลัง 24/7"
echo "• ตรวจสอบสถานะ: sudo systemctl status stockbot"
echo "• ดู Logs สด:    sudo journalctl -u stockbot -f"
echo "• หยุดการทำงาน: sudo systemctl stop stockbot"
echo "• เริ่มใหม่:     sudo systemctl restart stockbot"
echo "=========================================================="
