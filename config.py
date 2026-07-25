import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
TEXTS_DIR = DATA_DIR / "texts"
LOGS_DIR = DATA_DIR / "logs"
STORAGE_FILE = DATA_DIR / "storage.json"

for path in (DATA_DIR, SESSIONS_DIR, TEXTS_DIR, LOGS_DIR):
    path.mkdir(parents=True, exist_ok=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

GREETING_TEXT = os.getenv("GREETING_TEXT", "ЗДРАВСТВУЙТЕ")
REPORTS_PER_BOT = int(os.getenv("REPORTS_PER_BOT", "3"))
REPORTS_PER_MESSAGE = int(os.getenv("REPORTS_PER_MESSAGE", "3"))
CAMPAIGN_ROUNDS = int(os.getenv("CAMPAIGN_ROUNDS", "5"))
# пауза между раундами ~20 мин (как в видео)
ROUND_DELAY_MIN_SEC = int(os.getenv("ROUND_DELAY_MIN_SEC", "1200"))
ROUND_DELAY_MAX_SEC = int(os.getenv("ROUND_DELAY_MAX_SEC", "1260"))
# пауза между аккаунтами ~5 мин
ACCOUNT_DELAY_SEC = int(os.getenv("ACCOUNT_DELAY_SEC", "300"))
# не использовать аккаунты со спамблоком
SKIP_SPAMBLOCKED = os.getenv("SKIP_SPAMBLOCKED", "1").strip() not in ("0", "false", "False")
# проверять спамблок перед каждым заходом аккаунта в раунде
CHECK_SPAMBLOCK_EACH_ROUND = os.getenv("CHECK_SPAMBLOCK_EACH_ROUND", "1").strip() not in (
    "0",
    "false",
    "False",
)

BOT_TEXTS_FILE = TEXTS_DIR / "bot_reports.txt"
MESSAGE_TEXTS_FILE = TEXTS_DIR / "message_reports.txt"

PHRASE_FILES = {
    "ru": TEXTS_DIR / "phrases_ru.txt",
    "en": TEXTS_DIR / "phrases_en.txt",
}
LANGUAGE_LABELS = {
    "ru": "Русский",
    "en": "English",
}

# Категории жалоб (выбор в боте перед стартом кампании)
REPORT_CATEGORIES = [
    {
        "id": "illegal_docs",
        "title": "Незаконные товары / подделка документов",
        "short_title": "Подделка документов",
        "peer_reason": "other",
        "keywords": (
            "незакон",
            "illegal",
            "товар",
            "услуг",
            "поддел",
            "документ",
            "forgery",
            "goods",
            "services",
        ),
    },
    {
        "id": "fraud",
        "title": "Мошенничество / ложные фин. обещания",
        "short_title": "Мошенничество",
        "peer_reason": "fake",
        "keywords": (
            "мошен",
            "fraud",
            "scam",
            "финанс",
            "ложн",
            "обещан",
            "фишинг",
            "phishing",
            "продавец",
            "fake",
        ),
    },
]

# Виды жалоб в меню: обе категории сразу = "both"
REPORT_TYPE_CHOICES = ("illegal_docs", "fraud", "both")
DEFAULT_REPORT_CATEGORY = "illegal_docs"
BOTH_CATEGORY_ID = "both"


def get_report_category(category_id: str | None) -> dict:
    by_id = {c["id"]: c for c in REPORT_CATEGORIES}
    if category_id == BOTH_CATEGORY_ID:
        return REPORT_CATEGORIES[0]
    return by_id.get(category_id or "") or by_id[DEFAULT_REPORT_CATEGORY]


def get_report_categories(category_id: str | None) -> list[dict]:
    """Одна или обе категории для кампании."""
    if (category_id or "") == BOTH_CATEGORY_ID:
        return list(REPORT_CATEGORIES)
    return [get_report_category(category_id)]


def report_categories_label(category_id: str | None) -> str:
    cats = get_report_categories(category_id)
    return " + ".join(c.get("short_title") or c["title"] for c in cats)

DEFAULT_BOT_REPORTS = [
    "Подделка документов, незаконные услуги.",
    "Illegal goods and forged documents.",
    "Бот продаёт поддельные документы.",
    "Наркотики / психотропные вещества — реклама.",
    "Promotes illegal drugs / controlled substances.",
    "Мошенничество: ложные финансовые обещания.",
    "False financial promises / scam.",
    "Сомнительный продавец поддельных документов.",
    "Fraud and document forgery.",
    "Жалоба: мошенничество и обман пользователей.",
]

DEFAULT_MESSAGE_REPORTS = [
    "Сообщение рекламирует поддельные документы.",
    "Illegal goods / forged documents in this message.",
    "Сообщение связано с наркотиками / психотропными.",
    "Message promotes illegal drugs.",
    "Мошенничество: ложные финансовые обещания.",
    "False financial promises in the message.",
    "Фишинг / обман в ответе бота.",
    "Scam / phishing reply.",
    "Сомнительный продавец, товар, услуга.",
    "Questionable seller / illegal service offer.",
]
