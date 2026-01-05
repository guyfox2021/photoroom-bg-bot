import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PHOTOROOM_API_KEY = os.getenv("PHOTOROOM_API_KEY", "")

# Админ (только он видит /stats и /admin)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

# (опционально) лимит веса файла, если хочешь использовать позже
MAX_MB = int(os.getenv("MAX_MB", "12") or "12")
