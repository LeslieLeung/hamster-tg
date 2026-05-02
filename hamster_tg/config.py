import logging
import os
import re
from pathlib import Path


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.environ.get("TELEGRAM_API_BASE_URL", "http://telegram-bot-api:8081/bot")
BASE_FILE_URL = os.environ.get(
    "TELEGRAM_API_BASE_FILE_URL", "http://telegram-bot-api:8081/file/bot"
)
DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "/downloads"))

FOLDER_NAME_RE = re.compile(r"^[\w\u4e00-\u9fff\u3400-\u4dbf-]+$")

# PTB wiki recommends timer-based media-group collection. Keep this in 1-2s range.
MEDIA_GROUP_ACK_DELAY_SECONDS = 1.5
DOWNLOAD_MAX_RETRIES = 6
RETRY_BACKOFF_BASE_SECONDS = 0.5
GET_FILE_READ_TIMEOUT_SECONDS = 20.0
# Set to None to disable read timeout for large/slow media downloads.
DOWNLOAD_FILE_READ_TIMEOUT_SECONDS: float | None = None
REQUEST_CONNECT_TIMEOUT_SECONDS = 10.0
REQUEST_POOL_TIMEOUT_SECONDS = 30.0
API_REQUEST_MIN_INTERVAL_SECONDS = 0.35

