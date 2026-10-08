import logging
import os
import re
import time
from html import escape
from urllib.parse import urljoin

import psycopg
import requests
from bs4 import BeautifulSoup


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required. Add it in Replit Secrets.")
if not DATABASE_URL:
    raise SystemExit("DATABASE_URL is required for persistent bot data.")

try:
    POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))
except ValueError as exc:
    raise SystemExit("POLL_SECONDS must be a whole number of seconds.") from exc
if POLL_SECONDS < 60:
    raise SystemExit("POLL_SECONDS must be at least 60 seconds.")

OLX_URL = os.getenv(
    "OLX_URL",
    "https://www.olx.kz/elektronika/telefony-i-aksesuary/alm/"
    "?search[order]=created_at:desc",
).strip()
SEND_EXISTING_ON_START = os.getenv("SEND_EXISTING_ON_START", "0").strip() == "1"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
HEADERS = {
    "User-Agent": "OLX-Telegram-Monitor/1.0",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("olx-bot")


class TelegramAPIError(RuntimeError):
    def __init__(self, description, error_code=None, retry_after=None):
        super().__init__(description)
        self.error_code = error_code
        self.retry_after = retry_after


def safe_error(error):
    message = str(error)
    for secret in (BOT_TOKEN, DATABASE_URL):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def connect_db():
    return psycopg.connect(
        DATABASE_URL,
        connect_timeout=15,
        application_name="olx-telegram-monitor",
    )


def check_database_schema():
    failures = 0
    while True:
        try:
            with connect_db() as con:
                con.execute(
                    "CREATE TABLE IF NOT EXISTS olx_bot_chats (chat_id BIGINT PRIMARY KEY)"
                )
                con.execute(
                    "CREATE TABLE IF NOT EXISTS olx_seen_ads (listing_url TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
                )
                con.commit()
            log.info("Database tables verified/created successfully.")
            return
        except psycopg.OperationalError as exc:
            failures += 1
            delay = retry_delay(exc, failures)
            log.warning(
                "Database is unavailable; retrying in %s seconds: %s",
                delay,
                safe_error(exc),
            )
            time.sleep(delay)

def telegram(method, payload=None, timeout=30):
    response = requests.post(
        f"{TELEGRAM_API}/{method}",
        json=payload or {},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        parameters = data.get("parameters") or {}
        raise TelegramAPIError(
            data.get("description", "Telegram API request failed"),
            error_code=data.get("error_code"),
            retry_after=parameters.get("retry_after"),
        )
    return data


def send_text(chat_id, text):
    telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
    )


def send_photo(chat_id, photo, caption):
    telegram(
        "sendPhoto",
        {
            "chat_id": chat_id,
            "photo": photo,
            "caption": caption[:1024],
            "parse_mode": "HTML",
        },
    )


def normalize_url(href):
    if not href:
        return None
    return urljoin("https://www.olx.kz", href).split("#")[0]


def clean_lines(text):
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and line not in lines:
            lines.append(line)
    return lines


def parse_price(lines):
    for line in lines:
        if re.search(r"\b(тг|KZT)\b", line, re.I):
            return line
    return "Цена не указана"


def parse_card(card):
    links = card.select('a[href*="/d/"], a[href*="/oferta/"]')
    if not links:
        return None

    link = links[0]
    url = normalize_url(link.get("href"))
    if not url:
        return None

    title = (
        link.get("title")
        or link.get_text(" ", strip=True)
        or ""
    ).strip()
    if not title:
        for selector in ("h4", "h5", "h6", "strong"):
            node = card.select_one(selector)
            if node:
                title = node.get_text(" ", strip=True)
                if title:
                    break

    lines = clean_lines(card.get_text("\n", strip=True))
    image = None
    img = card.select_one("img")
    if img:
        image = img.get("src") or img.get("data-src")

    return {
        "url": url,
        "title": title or "Новое объявление OLX",
        "price": parse_price(lines),
        "image": image,
    }


def fetch_ads():
    response = requests.get(OLX_URL, headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    cards = soup.select('[data-cy="l-card"]')

    if not cards:
        cards = []
        seen_nodes = set()
        for link in soup.select('a[href*="/d/"], a[href*="/oferta/"]'):
            node = link
            for _ in range(4):
                node = node.parent if node else None
                if not node:
                    break
                if id(node) not in seen_nodes:
                    cards.append(node)
                    seen_nodes.add(id(node))

    ads = []
    seen_urls = set()
    for card in cards:
        ad = parse_card(card)
        if ad and ad["url"] not in seen_urls:
            ads.append(ad)
            seen_urls.add(ad["url"])
    return ads


def format_ad(ad):
    return (
        "🚨 <b>ЖАҢА ОБЪЯВЛЕНИЕ</b>\n\n"
        f"📱 <b>{escape(ad['title'])}</b>\n"
        f"💰 {escape(ad['price'])}\n\n"
        f"🔗 <a href=\"{escape(ad['url'], quote=True)}\">Ашу: OLX</a>"
    )


def add_chat(chat_id):
    with connect_db() as con:
        con.execute(
            "INSERT INTO olx_bot_chats(chat_id) VALUES (%s) "
            "ON CONFLICT (chat_id) DO NOTHING",
            (chat_id,),
        )


def get_chats():
    with connect_db() as con:
        rows = con.execute("SELECT chat_id FROM olx_bot_chats").fetchall()
    return [row[0] for row in rows]


def has_seen_ads():
    with connect_db() as con:
        row = con.execute("SELECT EXISTS(SELECT 1 FROM olx_seen_ads)").fetchone()
    return bool(row[0])


def mark_seen_ads(ads):
    if not ads:
        return
    with connect_db() as con:
        con.executemany(
            "INSERT INTO olx_seen_ads(listing_url) VALUES (%s) "
            "ON CONFLICT (listing_url) DO NOTHING",
            [(ad["url"],) for ad in ads],
        )


def bootstrap():
    ads = fetch_ads()
    first_run = not has_seen_ads()
    log.info("Initial scan found %s ads; first run: %s", len(ads), first_run)
    if not first_run:
        return []

    # Store the baseline before notifying so a restart cannot resend it.
    mark_seen_ads(ads)
    return ads if SEND_EXISTING_ON_START else []


def scan_once():
    ads = fetch_ads()
    if not ads:
        return []

    urls = list(dict.fromkeys(ad["url"] for ad in ads))
    with connect_db() as con:
        rows = con.execute(
            "SELECT listing_url FROM olx_seen_ads WHERE listing_url = ANY(%s)",
            (urls,),
        ).fetchall()
        known_urls = {row[0] for row in rows}
        new_ads = [ad for ad in ads if ad["url"] not in known_urls]
        if new_ads:
            con.executemany(
                "INSERT INTO olx_seen_ads(listing_url) VALUES (%s) "
                "ON CONFLICT (listing_url) DO NOTHING",
                [(ad["url"],) for ad in new_ads],
            )

    new_ads.reverse()
    return new_ads


def handle_updates(offset):
    response = telegram(
        "getUpdates",
        {
            "offset": offset,
            "timeout": 20,
            "allowed_updates": ["message"],
        },
        timeout=30,
    )
    return response.get("result", [])


def retry_delay(error, failure_count):
    base = max(POLL_SECONDS, 60)
    delay = min(base * (2 ** min(failure_count - 1, 5)), 300)
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in (403, 429) or getattr(error, "error_code", None) in (403, 429):
        delay = max(delay, 60)
    try:
        retry_after = int(getattr(error, "retry_after", 0) or 0)
    except (TypeError, ValueError):
        retry_after = 0
    return min(max(delay, retry_after), 3600)


def is_retryable_error(error):
    error_code = getattr(error, "error_code", None)
    if error_code is not None:
        return error_code == 429 or error_code >= 500

    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is not None:
        return status_code == 429 or status_code >= 500

    return isinstance(error, (requests.RequestException, psycopg.OperationalError))


def authenticate_bot():
    failures = 0
    while True:
        try:
            return telegram("getMe")["result"]
        except Exception as exc:
            error_code = getattr(exc, "error_code", None)
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if error_code in (400, 401, 403) or status_code in (400, 401, 403):
                raise SystemExit(
                    f"Could not authenticate with Telegram Bot API: {safe_error(exc)}"
                ) from None

            failures += 1
            delay = retry_delay(exc, failures)
            log.warning(
                "Telegram startup check failed; retrying in %s seconds: %s",
                delay,
                safe_error(exc),
            )
            time.sleep(delay)


def send_ad_to_chats(ad, chat_ids):
    caption = format_ad(ad)
    for chat_id in chat_ids:
        failures = 0
        while True:
            try:
                if ad["image"]:
                    send_photo(chat_id, ad["image"], caption)
                else:
                    send_text(chat_id, caption)
                break
            except Exception as exc:
                if not is_retryable_error(exc) or failures >= 2:
                    log.warning(
                        "Failed to send listing %s to chat %s: %s",
                        ad["url"],
                        chat_id,
                        safe_error(exc),
                    )
                    break
                failures += 1
                delay = retry_delay(exc, failures)
                log.warning(
                    "Telegram send failed; retrying in %s seconds: %s",
                    delay,
                    safe_error(exc),
                )
                time.sleep(delay)


def main():
    check_database_schema()
    bot_info = authenticate_bot()

    log.info("Telegram bot authenticated as @%s", bot_info.get("username", "unknown"))
    offset = 0
    telegram_failures = 0
    next_telegram_poll_at = 0

    scan_failures = 0
    next_scan_at = time.time()
    try:
        initial_ads = bootstrap()
        next_scan_at = time.time() + POLL_SECONDS
        if initial_ads:
            send_existing_to = get_chats()
            for ad in reversed(initial_ads):
                send_ad_to_chats(ad, send_existing_to)
    except Exception as exc:
        scan_failures = 1
        next_scan_at = time.time() + retry_delay(exc, scan_failures)
        log.warning(
            "Initial OLX scan failed; retrying in %s seconds: %s",
            int(next_scan_at - time.time()),
            safe_error(exc),
        )

    log.info("OLX monitor started. Poll interval: %s seconds.", POLL_SECONDS)

    while True:
        if time.time() >= next_telegram_poll_at:
            try:
                for update in handle_updates(offset):
                    update_id = update.get("update_id", offset)
                    message = update.get("message") or {}
                    chat_id = (message.get("chat") or {}).get("id")
                    text = (message.get("text") or "").strip()
                    if not chat_id:
                        offset = max(offset, update_id + 1)
                        continue

                    add_chat(chat_id)
                    if text.startswith("/start"):
                        send_text(
                            chat_id,
                            "✅ <b>OLX монитор запущен</b>\n\n"
                            "Фильтр:\n"
                            "• Телефоны и аксессуары\n"
                            "• Алматинская область\n"
                            "• Самые новые\n"
                            "• Все объявления\n\n"
                            f"⏱ Проверка каждые {POLL_SECONDS} сек.",
                        )
                    elif text.startswith("/status"):
                        send_text(
                            chat_id,
                            "🟢 <b>Статус</b>\n"
                            "Телефоны и аксессуары\n"
                            "Алматинская область\n"
                            "Самые новые\n"
                            f"Проверка: {POLL_SECONDS} сек.",
                        )
                    offset = max(offset, update_id + 1)
                telegram_failures = 0
                next_telegram_poll_at = time.time()
            except Exception as exc:
                telegram_failures += 1
                delay = retry_delay(exc, telegram_failures)
                next_telegram_poll_at = time.time() + delay
                log.warning(
                    "Telegram polling failed; retrying in %s seconds: %s",
                    delay,
                    safe_error(exc),
                )

        if time.time() >= next_scan_at:
            try:
                new_ads = scan_once()
                scan_failures = 0
                next_scan_at = time.time() + POLL_SECONDS
                if new_ads:
                    chat_ids = get_chats()
                    log.info(
                        "Found %s new listings for %s chats.",
                        len(new_ads),
                        len(chat_ids),
                    )
                    for ad in new_ads:
                        send_ad_to_chats(ad, chat_ids)
                else:
                    log.info("No new listings.")
            except Exception as exc:
                scan_failures += 1
                delay = retry_delay(exc, scan_failures)
                next_scan_at = time.time() + delay
                log.warning(
                    "OLX scan failed; retrying in %s seconds: %s",
                    delay,
                    safe_error(exc),
                )

        time.sleep(1)


if __name__ == "__main__":
    main()
