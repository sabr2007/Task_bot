import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var is not set")

DB_PATH = Path(os.getenv("DB_PATH", "tasks.db"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")
