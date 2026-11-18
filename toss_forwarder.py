import asyncio
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, events
from dotenv import load_dotenv
import os

# ─────────────────────────────────────────────
# 0. Load environment variables
# ─────────────────────────────────────────────
load_dotenv()

API_ID        = int(os.getenv("API_ID", "0"))
API_HASH      = os.getenv("API_HASH", "")
PHONE         = os.getenv("PHONE", "")
TARGET_CHAN   = os.getenv("TARGET_CHANNEL", "")
SESSION_NAME  = os.getenv("SESSION_NAME", "toss_session")
SOURCE_CHAN   = os.getenv("SOURCE_CHANNEL", "")

MAX_MSG_LEN       = 100
DEDUP_EXPIRY_SECS = 5 * 60   # 5 minutes

# ─────────────────────────────────────────────
# 1. Logging
# ─────────────────────────────────────────────
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "forwarder.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 2. Duplicate prevention (5 minute expiry)
# ─────────────────────────────────────────────
_seen_keys: dict[str, float] = {}

def _is_seen(key: str) -> bool:
    ts = _seen_keys.get(key)
    if ts is None:
        return False
    if time.time() - ts > DEDUP_EXPIRY_SECS:
        del _seen_keys[key]
        return False
    return True

def _mark_seen(key: str) -> None:
    _seen_keys[key] = time.time()

def _toss_key(text: str) -> str | None:
    clean = text.encode("ascii", "ignore").decode("ascii").lower()
    clean = re.sub(r"[^a-z0-9\s]", " ", clean)
    clean = " ".join(clean.split())

    match = re.search(r"^(.*?)\s+won\s+the\s+toss", clean)
    if not match:
        return None
    team = re.sub(r"\s+", "", match.group(1))

    decision_match = re.search(r"\b(bat|bowl|court)\b", clean)
    if not decision_match:
        return None

    return f"{team}|{decision_match.group(1)}"

# ─────────────────────────────────────────────
# 3. Filter
# ─────────────────────────────────────────────
_TOSS_PHRASE = re.compile(
    r"WON\s+THE\s+TOSS\s+(AND\s+)?(DECIDED|CHOSE|OPTED?|ELECTED|CALLED|SELECTED)\s+(TO\s+)?",
    re.IGNORECASE
)
_DECISION = re.compile(r"\b(BAT|BOWL|COURT)\b", re.IGNORECASE)
_ENDING   = re.compile(r"[✔✅✓☑]")

def is_toss_message(text: str) -> bool:
    if not text:
        return False

    visible = re.sub(r"[\U000E0000-\U000E007F]", "", text.strip())
    if len(visible) > MAX_MSG_LEN:
        return False

    normalised = " ".join(text.split())

    if len(_TOSS_PHRASE.findall(normalised)) > 1:
        return False

    has_phrase   = bool(_TOSS_PHRASE.search(normalised))
    has_decision = bool(_DECISION.search(normalised))
    has_tick     = bool(_ENDING.search(text))
    has_first    = bool(re.search(r"\b(BAT|BOWL|COURT)\b", normalised, re.IGNORECASE))

    return has_phrase and has_decision and (has_tick or has_first)

# ─────────────────────────────────────────────
# 4. Format output
# ─────────────────────────────────────────────
def _format_output(text: str) -> str:
    clean = text.strip().replace("*", "")
    clean = " ".join(clean.split())
    return f"<b>{clean}</b>"

# ─────────────────────────────────────────────
# 5. Client
# ─────────────────────────────────────────────
client = TelegramClient(
    SESSION_NAME,
    API_ID,
    API_HASH,
    connection_retries=None,
    auto_reconnect=True,
    flood_sleep_threshold=60,
)

# ─────────────────────────────────────────────
# 6. Send helper (runs in background)
# ─────────────────────────────────────────────
async def _send(formatted: str) -> None:
    try:
        await client.send_message(
            entity=TARGET_CHAN,
            message=formatted,
            parse_mode="html",
        )
        log.info("✅ Sent at %s: %s", datetime.now().isoformat(timespec="seconds"), formatted[:80])
    except Exception as exc:
        log.error("❌ Send error: %s", exc)

# ─────────────────────────────────────────────
# 7. Handler — no entity lookup, filter by ID
# ─────────────────────────────────────────────
SOURCE_ID: int = 0   # set in main() after parsing SOURCE_CHAN

async def handle_new_message(event: events.NewMessage.Event) -> None:
    # Fast numeric check — no entity resolution needed
    if event.chat_id != SOURCE_ID:
        return

    text = event.message.text or ""

    if not is_toss_message(text):
        return

    toss_key = _toss_key(text)
    if not toss_key:
        return

    if _is_seen(toss_key):
        log.info("Duplicate skipped (key: %s).", toss_key)
        return

    _mark_seen(toss_key)

    # Fire and forget — handler returns immediately
    asyncio.create_task(_send(_format_output(text)))

# ─────────────────────────────────────────────
# 8. Main
# ─────────────────────────────────────────────
async def main() -> None:
    global SOURCE_ID

    if not all([API_ID, API_HASH, PHONE, SOURCE_CHAN, TARGET_CHAN]):
        log.critical("Missing config. Need API_ID, API_HASH, PHONE, SOURCE_CHANNEL, TARGET_CHANNEL in .env")
        return

    # Parse numeric channel ID from SOURCE_CHANNEL env var
    try:
        SOURCE_ID = int(SOURCE_CHAN)
    except ValueError:
        log.critical("SOURCE_CHANNEL must be a numeric ID (e.g. -1003988437611), got: %s", SOURCE_CHAN)
        return

    log.info("🚀 Starting…")
    log.info("Source ID : %s", SOURCE_ID)
    log.info("Target    : %s", TARGET_CHAN)

    await client.start(phone=PHONE)

    # Register handler on ALL messages — filter by chat_id inside handler.
    # No entity lookup, no dialog scan — bot is live instantly after login.
    client.add_event_handler(handle_new_message, events.NewMessage())

    log.info("🟢 Ready. Listening on chat_id=%s. Dedup window: %d min.", SOURCE_ID, DEDUP_EXPIRY_SECS // 60)
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
