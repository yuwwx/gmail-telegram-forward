import base64
import json
import logging
import os
import signal
import sys
import time
from email.header import decode_header, make_header

import requests
from dotenv import load_dotenv

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# ============================================================
# Configuration
# ============================================================

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly"
]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Ops/admin chat: errors and service status. Falls back to
# the main chat when not configured.
_admin_chat_id = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip()
TELEGRAM_ADMIN_CHAT_ID = _admin_chat_id or TELEGRAM_CHAT_ID

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "30"))

ALLOWED_DOMAINS = [
    domain.strip().lstrip("@").lower()
    for domain in os.getenv(
        "ALLOWED_DOMAINS",
        ""
    ).split(",")
    if domain.strip()
]


# ============================================================
# Paths
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

TOKEN_FILE = os.path.join(
    BASE_DIR,
    "token.json"
)

CREDENTIALS_FILE = os.path.join(
    BASE_DIR,
    "credentials.json"
)

STATE_FILE = os.path.join(
    BASE_DIR,
    "state.json"
)


# ============================================================
# Logging
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("gmail-telegram")


# ============================================================
# Gmail authentication
# ============================================================

def get_credentials():
    creds = None

    if os.path.exists(TOKEN_FILE):
        logger.info(
            "Loading Gmail credentials from %s",
            TOKEN_FILE
        )

        creds = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES
        )

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing Gmail access token")

        creds.refresh(Request())

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    if not creds or not creds.valid:
        logger.info(
            "Gmail credentials are missing or invalid. "
            "Starting OAuth authorization."
        )

        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE,
            SCOPES
        )

        creds = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent"
        )

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

        logger.info(
            "Gmail OAuth credentials saved to %s",
            TOKEN_FILE
        )

    return creds


# ============================================================
# State
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        logger.info(
            "State file does not exist. Starting with empty state."
        )

        return set()

    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)

        processed = set(
            data.get("processed", [])
        )

        logger.info(
            "Loaded state: %d processed messages",
            len(processed)
        )

        return processed

    except Exception:
        logger.exception(
            "Failed to load state file. "
            "Starting with empty state."
        )

        return set()


def save_state(processed):
    # Keep only the last 5000 message IDs.
    processed_list = list(processed)[-5000:]

    tmp_file = STATE_FILE + ".tmp"

    with open(tmp_file, "w") as f:
        json.dump(
            {
                "processed": processed_list
            },
            f,
            indent=2
        )

    os.replace(
        tmp_file,
        STATE_FILE
    )


# ============================================================
# Gmail query
# ============================================================

def build_gmail_query():
    """
    Builds Gmail search query.

    Example:

    is:unread {from:(@example.com) from:(@example.org)}
    """

    domains = []

    for domain in ALLOWED_DOMAINS:
        domain = domain.strip().lower()

        if not domain:
            continue

        # Remove accidental @ from configuration.
        domain = domain.lstrip("@")

        domains.append(
            f"from:(@{domain})"
        )

    if not domains:
        raise RuntimeError(
            "ALLOWED_DOMAINS is empty. "
            "Add at least one domain."
        )

    domain_query = " OR ".join(domains)

    query = f"is:unread {{{domain_query}}}"

    return query


# ============================================================
# Gmail helpers
# ============================================================

def decode_mime_header(value):
    if not value:
        return ""

    try:
        return str(
            make_header(
                decode_header(value)
            )
        )

    except Exception:
        return value


def decode_body(data):
    if not data:
        return ""

    try:
        return base64.urlsafe_b64decode(
            data + "=" * (-len(data) % 4)
        ).decode(
            "utf-8",
            errors="replace"
        )

    except Exception:
        return ""


def extract_text(payload):
    mime_type = payload.get(
        "mimeType",
        ""
    )

    if mime_type == "text/plain":
        body = payload.get(
            "body",
            {}
        )

        return decode_body(
            body.get("data")
        )

    parts = payload.get(
        "parts",
        []
    )

    for part in parts:
        text = extract_text(part)

        if text:
            return text

    return ""


def get_header(headers, name):
    for header in headers:
        if header["name"].lower() == name.lower():
            return decode_mime_header(
                header["value"]
            )

    return ""


def get_message(service, message_id):
    return service.users().messages().get(
        userId="me",
        id=message_id,
        format="full"
    ).execute()


# ============================================================
# Telegram
# ============================================================

def send_telegram(text, chat_id=None):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
        },
        timeout=30,
    )

    response.raise_for_status()


def notify_admin(text):
    """
    Safely sends a notification to the admin chat.
    """
    try:
        send_telegram(text, TELEGRAM_ADMIN_CHAT_ID)
    except Exception:
        logger.exception(
            "Failed to send error notification to Telegram"
        )


# ============================================================
# Message formatting
# ============================================================

def format_message(message):
    payload = message.get(
        "payload",
        {}
    )

    headers = payload.get(
        "headers",
        []
    )

    sender = get_header(
        headers,
        "From"
    )

    subject = get_header(
        headers,
        "Subject"
    )

    date = get_header(
        headers,
        "Date"
    )

    body = extract_text(
        payload
    ).strip()

    # Telegram sendMessage has a 4096-character limit.
    # Leave some space for the header.
    if len(body) > 3500:
        body = (
            body[:3500]
            + "\n\n[текст обрезан]"
        )

    result = (
        "📧 Новое письмо\n\n"
        f"От: {sender}\n"
        f"Тема: {subject}\n"
        f"Дата: {date}\n"
    )

    if body:
        result += (
            f"\n{body}"
        )

    return result


# ============================================================
# Gmail polling
# ============================================================

def poll_gmail(service, processed):
    query = build_gmail_query()

    logger.info(
        "Polling Gmail: query=%s",
        query
    )

    result = service.users().messages().list(
        userId="me",
        q=query,
        maxResults=50
    ).execute()

    messages = result.get(
        "messages",
        []
    )

    logger.info(
        "Gmail poll completed: %d matching messages",
        len(messages)
    )

    if not messages:
        logger.info(
            "No new matching messages"
        )

        return

    new_messages = [
        message
        for message in messages
        if message["id"] not in processed
    ]

    if not new_messages:
        logger.info(
            "No new messages to forward. "
            "All matching messages were already processed."
        )

        return

    logger.info(
        "Found %d new message(s) to forward",
        len(new_messages)
    )

    # Gmail returns newest first.
    # Reverse so Telegram receives them oldest → newest.
    for item in reversed(new_messages):

        message_id = item["id"]

        logger.info(
            "Processing Gmail message: %s",
            message_id
        )

        try:
            message = get_message(
                service,
                message_id
            )

            payload = message.get(
                "payload",
                {}
            )

            headers = payload.get(
                "headers",
                []
            )

            sender = get_header(
                headers,
                "From"
            )

            subject = get_header(
                headers,
                "Subject"
            )

            logger.info(
                "Message: id=%s from=%s subject=%s",
                message_id,
                sender,
                subject
            )

            telegram_text = format_message(
                message
            )

            # Company chat.
            send_telegram(
                telegram_text
            )

            # Admin chat also receives the mail. A failure of
            # this copy must not fail the whole processing,
            # otherwise the message would be re-sent to the
            # company chat on the next poll.
            try:
                send_telegram(
                    telegram_text,
                    TELEGRAM_ADMIN_CHAT_ID
                )

            except Exception:
                logger.exception(
                    "Failed to copy message to admin chat: %s",
                    message_id
                )

            logger.info(
                "Telegram message sent successfully: %s",
                message_id
            )

            # Only mark as processed AFTER successful
            # Telegram delivery.
            processed.add(
                message_id
            )

            save_state(
                processed
            )

        except Exception:
            logger.exception(
                "Failed to process message: %s",
                message_id
            )

            # Do not add message to processed state.
            # It will be retried on the next poll.


# ============================================================
# Main
# ============================================================

def main():
    logger.info(
        "Starting Gmail → Telegram forwarder"
    )

    logger.info(
        "Poll interval: %d seconds",
        POLL_INTERVAL
    )

    logger.info(
        "Allowed domains: %s",
        ", ".join(ALLOWED_DOMAINS)
    )

    logger.info(
        "Gmail scope: %s",
        SCOPES[0]
    )

    processed = load_state()

    notify_admin(
        "🚀 Форвардер Gmail → Telegram запущен\n"
        f"Домены: {', '.join(ALLOWED_DOMAINS)}\n"
        f"Интервал опроса: {POLL_INTERVAL} сек."
    )

    def handle_stop(signum, frame):
        notify_admin(
            f"🛑 Форвардер остановлен (signal {signum})"
        )

        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    credentials = None
    service = None

    # Flap suppression: notify about a failure only once,
    # until a poll succeeds again.
    failure_notified = False

    while True:
        try:
            # Initialize only if it never succeeded before.
            if service is None:
                credentials = get_credentials()

                service = build(
                    "gmail",
                    "v1",
                    credentials=credentials
                )

                logger.info(
                    "Gmail API initialized successfully"
                )

            poll_gmail(
                service,
                processed
            )

            # Success: re-arm notifications.
            failure_notified = False

        except Exception as e:
            logger.exception(
                "Gmail polling failed"
            )

            if not failure_notified:
                notify_admin(
                    f"⚠️ Ошибка форвардера Gmail → Telegram:\n"
                    f"`{type(e).__name__}: {e}`"
                )

                failure_notified = True

        logger.info(
            "Next poll in %d seconds",
            POLL_INTERVAL
        )

        time.sleep(
            POLL_INTERVAL
        )


if __name__ == "__main__":
    main()
