# import os
# import requests


# TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# def send_telegram_alert(
#     event_type,
#     details,
#     severity,
#     source_name,
#     face_path=None,
#     body_path=None,
# ):
#     """
#     Send Sentinel AI alert to Telegram.

#     This function is intentionally separate from the AI processing loop.
#     It can safely run in a background worker.
#     """

#     if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
#         print("⚠️ Telegram is not configured.")
#         return False

#     message = (
#         "🚨 SENTINEL AI ALERT\n\n"
#         f"Event: {event_type}\n"
#         f"Camera: {source_name}\n"
#         f"Severity: {severity.upper()}\n\n"
#         f"{details}"
#     )

#     api_url = (
#         f"https://api.telegram.org/bot"
#         f"{TELEGRAM_BOT_TOKEN}/sendMessage"
#     )

#     try:
#         response = requests.post(
#             api_url,
#             data={
#                 "chat_id": TELEGRAM_CHAT_ID,
#                 "text": message,
#             },
#             timeout=5,
#         )

#         response.raise_for_status()

#         print(f"✅ Telegram alert sent: {event_type}")

#     except requests.RequestException as error:
#         print(f"⚠️ Telegram message failed: {error}")
#         return False

#     except Exception as error:
#         print(f"⚠️ Telegram error: {error}")
#         return False

#     # Send face evidence
#     if face_path:
#         send_telegram_photo(
#             face_path,
#             "📸 Face evidence captured by Sentinel AI."
#         )

#     # Send body evidence
#     if body_path:
#         send_telegram_photo(
#             body_path,
#             "🧍 Full-body evidence captured by Sentinel AI."
#         )

#     return True


# def send_telegram_photo(photo_path, caption=""):
#     """Send evidence image to Telegram."""

#     if not photo_path:
#         return False

#     if not os.path.exists(photo_path):
#         print(f"⚠️ Evidence file not found: {photo_path}")
#         return False

#     if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
#         return False

#     api_url = (
#         f"https://api.telegram.org/bot"
#         f"{TELEGRAM_BOT_TOKEN}/sendPhoto"
#     )

#     try:
#         with open(photo_path, "rb") as photo:

#             response = requests.post(
#                 api_url,
#                 data={
#                     "chat_id": TELEGRAM_CHAT_ID,
#                     "caption": caption,
#                 },
#                 files={
#                     "photo": photo,
#                 },
#                 timeout=10,
#             )

#         response.raise_for_status()

#         print(f"✅ Telegram evidence sent: {photo_path}")
#         return True

#     except requests.RequestException as error:
#         print(f"⚠️ Telegram photo failed: {error}")
#         return False

#     except Exception as error:
#         print(f"⚠️ Telegram photo error: {error}")
#         return False
    



import os
import requests
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_alert(
    event_type,
    details,
    severity,
    source_name,
    face_path=None,
    body_path=None,
):
    """Send Sentinel AI alert to Telegram."""

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram is not configured.")
        print("   BOT TOKEN:", bool(TELEGRAM_BOT_TOKEN))
        print("   CHAT ID:", bool(TELEGRAM_CHAT_ID))
        return False

    message = (
        "🚨 SENTINEL AI ALERT\n\n"
        f"Event: {event_type}\n"
        f"Camera: {source_name}\n"
        f"Severity: {severity.upper()}\n\n"
        f"{details}"
    )

    api_url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    try:
        response = requests.post(
            api_url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=5,
        )

        response.raise_for_status()

        print(f"✅ Telegram alert sent: {event_type}")

    except requests.RequestException as error:
        print(f"⚠️ Telegram message failed: {error}")
        return False

    except Exception as error:
        print(f"⚠️ Telegram error: {error}")
        return False

    # Face evidence
    if face_path:
        send_telegram_photo(
            face_path,
            "📸 Face evidence captured by Sentinel AI.",
        )

    # Body evidence
    if body_path:
        send_telegram_photo(
            body_path,
            "🧍 Full-body evidence captured by Sentinel AI.",
        )

    return True


def send_telegram_photo(photo_path, caption=""):
    """Send evidence image to Telegram."""

    if not photo_path:
        return False

    if not os.path.exists(photo_path):
        print(f"⚠️ Evidence file not found: {photo_path}")
        return False

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram credentials missing.")
        return False

    api_url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendPhoto"
    )

    try:
        with open(photo_path, "rb") as photo:
            response = requests.post(
                api_url,
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "caption": caption,
                },
                files={
                    "photo": photo,
                },
                timeout=10,
            )

        response.raise_for_status()

        print(f"✅ Telegram evidence sent: {photo_path}")
        return True

    except requests.RequestException as error:
        print(f"⚠️ Telegram photo failed: {error}")
        return False

    except Exception as error:
        print(f"⚠️ Telegram photo error: {error}")
        return False


def _telegram_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %I:%M %p")
    except (TypeError, ValueError):
        return value or "Unavailable"


def _alert_caption(event_type, details, severity, source_name, event_id, evidence_hash, detected_at, hash_basis, blockchain="Not registered yet", transaction_hash=None, explorer_url=None):
    lines = [
        "SENTINELAI SECURITY ALERT",
        "",
        f"Event: {event_type}",
        f"Detected at: {_telegram_time(detected_at)}",
        f"Camera: {source_name}",
        f"Severity: {severity.upper()}",
        "",
        f"Evidence ID: {event_id}",
        f"Evidence SHA-256: {evidence_hash}",
        f"Hash basis: {hash_basis}",
        f"Blockchain: {blockchain}",
    ]
    if transaction_hash:
        lines.append(f"Transaction: {transaction_hash}")
    if explorer_url and transaction_hash:
        lines.append(f"MSTScan: {explorer_url.rstrip('/')}/tx/{transaction_hash}")
    if details:
        lines.extend(["", details])
    return "\n".join(lines)


def send_telegram_alert(event_type, details, severity, source_name, evidence_path=None, event_id=None, evidence_hash=None, detected_at=None, hash_basis="Evidence file"):
    """Send the exact local evidence identity with the first evidence image alert."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram is not configured.")
        return False
    caption = _alert_caption(event_type, details, severity, source_name, event_id, evidence_hash, detected_at, hash_basis)
    if evidence_path:
        return send_telegram_photo(evidence_path, caption)
    return _send_telegram_message(caption)


def _send_telegram_message(message: str) -> bool:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        print(f"Telegram message failed: {error}")
        return False


def send_telegram_blockchain_update(event: dict):
    """Notify the existing alert thread only after backend MST verification succeeds."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    explorer_url = os.getenv("MST_EXPLORER_URL", "")
    caption = _alert_caption(
        event["event_type"], event.get("details", ""), event["severity"], event["camera"],
        event["event_id"], event["evidence_hash"], event.get("created_at"), "Evidence file or recorded event fallback",
        blockchain="Confirmed and verified on MST Testnet", transaction_hash=event.get("blockchain_tx"), explorer_url=explorer_url,
    )
    return _send_telegram_message(caption)
