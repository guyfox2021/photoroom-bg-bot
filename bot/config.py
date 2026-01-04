from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PHOTOROOM_API_KEY = os.getenv("PHOTOROOM_API_KEY")
MAX_MB = int(os.getenv("MAX_MB", 12))
