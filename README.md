```
sudo mkdir -p /opt/gmail-telegram-forward
sudo chown -R $USER:$USER /opt/gmail-telegram-forward

cd /opt/gmail-telegram-forward

python3 -m venv .venv

source .venv/bin/activate

pip install -r requirements.txt
```

sudo nano /etc/systemd/system/gmail-telegram-forward.service

sudo systemctl daemon-reload
sudo systemctl enable --now gmail-telegram-forward
