from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.logging_config import configure_logging
from src.settings import database_path

load_dotenv(Path(__file__).with_name(".env"), override=False)
configure_logging()
logger = logging.getLogger("binance-square")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))
BUFFER_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN", "")
BINANCE_API_KEY = os.environ.get("BINANCE_SQUARE_API_KEY", "")
IMGBB_API_KEY = os.environ.get("IMGBB_API_KEY", "")

SCHEDULE_MIN_HOURS = float(os.environ.get("SCHEDULE_MIN_HOURS", "1"))
SCHEDULE_MAX_HOURS = float(os.environ.get("SCHEDULE_MAX_HOURS", "240"))

BINANCE_USE_IMAGES = os.environ.get("BINANCE_USE_IMAGES", "1") not in ("0", "false", "False", "")
# Если upload картинки в Binance падает — публиковать ли пост голым текстом.
# По умолчанию OFF: пост остаётся в очереди и ретраит ПОЗЖЕ С КАРТИНКОЙ, а не теряет её.
BINANCE_IMAGE_FALLBACK_TEXT = os.environ.get("BINANCE_IMAGE_FALLBACK_TEXT", "0") not in ("0", "false", "False", "")

# Dead-letter / backoff (борьба с бесконечным ретраем permanent-ошибок).
BINANCE_MAX_ATTEMPTS = int(os.environ.get("BINANCE_MAX_ATTEMPTS", "6"))
BINANCE_BACKOFF_BASE_SEC = int(os.environ.get("BINANCE_BACKOFF_BASE_SEC", "300"))      # 5 мин
BINANCE_BACKOFF_MAX_SEC = int(os.environ.get("BINANCE_BACKOFF_MAX_SEC", "21600"))      # 6 ч

HISTORY_LIMIT = int(os.environ.get("HISTORY_LIMIT", "500"))

# Опубликованные посты Binance держим в БД N дней (для истории/дебага), потом чистим.
# 0 = не чистить никогда.
BINANCE_PUBLISHED_TTL_DAYS = int(os.environ.get("BINANCE_PUBLISHED_TTL_DAYS", "30"))

# Раздел «Экосистема» в меню — ссылки на живые боты семейства (выключается в env).
ECOSYSTEM_LINKS_ENABLED = os.environ.get("ECOSYSTEM_LINKS_ENABLED", "1") not in ("0", "false", "False", "")

# Текст-плашка о переезде/закрытии бота: если задан — показывается в /start.
# Управляется через env без изменения кода (Coolify → restart).
SUNSET_NOTICE = os.environ.get("SUNSET_NOTICE", "").strip()

BUFFER_API = "https://api.buffer.com"
BINANCE_API_V1 = "https://www.binance.com/bapi/composite/v1/public/pgc/openApi"
BINANCE_API_V2 = "https://www.binance.com/bapi/composite/v2/public/pgc/openApi"
BINANCE_CLIENTTYPE = "binanceSkill"

DB_PATH = database_path()

SERVICE_EMOJI = {
    "twitter": "🐦", "linkedin": "💼", "threads": "🧵",
    "instagram": "📸", "facebook": "👤", "tiktok": "🎵",
    "mastodon": "🐘", "bluesky": "🦋", "pinterest": "📌",
}
