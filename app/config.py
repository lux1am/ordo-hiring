"""Конфигурация. Всё через переменные окружения, ничего в коде."""
import json
import logging
import os
import sys
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Модели ---------------------------------------------------------------
SCORER_MODEL = os.getenv("SCORER_MODEL", "claude-sonnet-4-6")
SCORER_MAX_TOKENS = int(os.getenv("SCORER_MAX_TOKENS", "4000"))

# --- Секреты --------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
SHEET_ID = os.getenv("SHEET_ID", "")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Данные")

# --- Пороги проверок ------------------------------------------------------
PASTE_PCT_THRESHOLD = float(os.getenv("PASTE_PCT_THRESHOLD", "30"))
SPEED_CPM_THRESHOLD_DESKTOP = float(os.getenv("SPEED_CPM_THRESHOLD_DESKTOP", "400"))
SPEED_CPM_THRESHOLD_MOBILE = float(os.getenv("SPEED_CPM_THRESHOLD_MOBILE", "700"))
SUSPICIOUS_MIN_LENGTH = int(os.getenv("SUSPICIOUS_MIN_LENGTH", "400"))
TAB_AWAY_SECONDS_THRESHOLD = float(os.getenv("TAB_AWAY_SECONDS_THRESHOLD", "60"))
AI_STYLE_DOWNGRADE_THRESHOLD = int(os.getenv("AI_STYLE_DOWNGRADE_THRESHOLD", "70"))
EVIDENCE_MAX_WORDS = int(os.getenv("EVIDENCE_MAX_WORDS", "15"))

# --- Пороги вердикта ------------------------------------------------------
VERDICT_A_MIN = int(os.getenv("VERDICT_A_MIN", "12"))
VERDICT_B_MIN = int(os.getenv("VERDICT_B_MIN", "9"))

# --- Антиспам -------------------------------------------------------------
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "5"))
HONEYPOT_FIELD = os.getenv("HONEYPOT_FIELD", "company_website")

# --- Ретраи Anthropic -----------------------------------------------------
API_RETRY_DELAYS = [
    float(x) for x in os.getenv("API_RETRY_DELAYS", "2,8,30").split(",") if x.strip()
]

# --- Восстановление зависших строк ---------------------------------------
STUCK_SCAN_INTERVAL_SEC = int(os.getenv("STUCK_SCAN_INTERVAL_SEC", "300"))   # 5 мин
STUCK_TIMEOUT_SEC = int(os.getenv("STUCK_TIMEOUT_SEC", "600"))               # 10 мин

# --- Статусы --------------------------------------------------------------
STATUS_NEW = "new"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_MANUAL = "manual"
STATUS_DUPLICATE = "duplicate"


def setup_logging() -> None:
    """Логи в stdout, одна строка на событие — так их видно в Render."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())


@lru_cache(maxsize=1)
def load_questions() -> dict:
    with open(BASE_DIR / "questions.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_system_prompt() -> str:
    """Системный промпт скорера + все пять рубрик, склеенные в один текст."""
    parts = [(BASE_DIR / "prompts" / "system.md").read_text(encoding="utf-8")]
    for i in range(1, 6):
        rubric = (BASE_DIR / "rubric" / f"q{i}.md").read_text(encoding="utf-8")
        parts.append(f"\n\n---\n\n{rubric}")
    return "".join(parts)


def missing_secrets() -> list[str]:
    """Какие обязательные переменные не заданы — для /health и старта."""
    missing = []
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if not GOOGLE_CREDENTIALS_JSON:
        missing.append("GOOGLE_CREDENTIALS_JSON")
    if not SHEET_ID:
        missing.append("SHEET_ID")
    return missing


# --- Время ----------------------------------------------------------------
# created_at пишется в местном времени рекрутёра, а не в UTC: колонку читают
# люди в таблице, и смещение на три часа там сбивает с толку.
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "3"))   # Москва
