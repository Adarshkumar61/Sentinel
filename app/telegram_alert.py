import os
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _telegram_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%d %b %Y, %I:%M:%S %p")
    except (TypeError, ValueError):
        return value or "Unavailable"


def format_alert_message(
    event_type: str,
    details: str,
    severity: str,
    source_name: str,
    event_id: str | None = None,
    evidence_hash: str | None = None,
    detected_at: str | None = None,
    blockchain_status: str = "⏳ PENDING",
    network: str = "MST Testnet",
    transaction_hash: str | None = None,
    contract_address: str | None = None,
) -> str:
    explorer_url = os.getenv("MST_EXPLORER_URL", "https://testnet.mstscan.com")
    lines = [
        "🚨 SENTINEL SECURITY EVENT",
        "",
        f"Event: {event_type}",
        f"Camera: {source_name}",
        f"Severity: {severity.upper()}",
        f"Time: {_telegram_time(detected_at or datetime.now().isoformat())}",
    ]
    if details:
        lines.append(f"Details: {details}")

    lines.extend([
        "",
        "🔐 BLOCKCHAIN PROOF",
        f"Status: {blockchain_status}",
        f"Network: {network}",
    ])

    if event_id:
        lines.append(f"Incident ID: {event_id}")
    if evidence_hash:
        lines.append(f"Evidence Hash: {evidence_hash}")
    if transaction_hash:
        lines.append(f"Transaction: {transaction_hash}")
    if contract_address:
        lines.append(f"Contract: {contract_address}")
    if transaction_hash and explorer_url:
        lines.append(f"MSTScan: {explorer_url.rstrip('/')}/tx/{transaction_hash}")

    return "\n".join(lines)


def send_telegram_alert(
    event_type: str,
    details: str,
    severity: str,
    source_name: str,
    face_path: str | None = None,
    body_path: str | None = None,
    evidence_path: str | None = None,
    event_id: str | None = None,
    evidence_hash: str | None = None,
    detected_at: str | None = None,
    blockchain_status: str = "⏳ PENDING",
    transaction_hash: str | None = None,
    contract_address: str | None = None,
    **kwargs,
) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram is not configured (TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing).")
        return False

    message = format_alert_message(
        event_type=event_type,
        details=details,
        severity=severity,
        source_name=source_name,
        event_id=event_id,
        evidence_hash=evidence_hash,
        detected_at=detected_at,
        blockchain_status=blockchain_status,
        transaction_hash=transaction_hash,
        contract_address=contract_address,
    )

    # Prioritize face evidence photo, then evidence_path, then body_path
    candidate_photo = face_path or evidence_path or body_path
    resolved_photo = None
    if candidate_photo:
        candidate_str = str(candidate_photo)
        if os.path.isfile(candidate_str):
            resolved_photo = candidate_str
        else:
            root_dir = Path(__file__).resolve().parent.parent
            file_name = Path(candidate_str).name
            possible_path = root_dir / "static" / "captures" / file_name
            if possible_path.is_file():
                resolved_photo = str(possible_path)
            else:
                possible_path2 = root_dir / candidate_str.lstrip("/\\")
                if possible_path2.is_file():
                    resolved_photo = str(possible_path2)

    if resolved_photo and os.path.isfile(resolved_photo):
        sent = send_telegram_photo(resolved_photo, caption=message)
        if sent:
            return True

    return _send_telegram_message(message)


def send_telegram_photo(photo_path: str, caption: str = "") -> bool:
    if not photo_path or not os.path.exists(photo_path):
        return False
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        with open(photo_path, "rb") as photo:
            response = requests.post(
                api_url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption[:1024]},
                files={"photo": photo},
                timeout=10,
            )
        response.raise_for_status()
        print(f"✅ Telegram photo evidence sent: {photo_path}")
        return True
    except Exception as error:
        print(f"⚠️ Telegram photo failed: {error}")
        return False


def _send_telegram_message(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        response = requests.post(
            api_url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
        response.raise_for_status()
        print("✅ Telegram text notification sent.")
        return True
    except Exception as error:
        print(f"⚠️ Telegram message failed: {error}")
        return False


def send_telegram_blockchain_update(event: dict) -> bool:
    """Send blockchain verification update with the same evidence photo."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    message = format_alert_message(
        event_type=event.get("event_type", "Security Alert"),
        details=event.get("details", ""),
        severity=event.get("severity", "warning"),
        source_name=event.get("camera", "Camera"),
        event_id=event.get("event_id"),
        evidence_hash=event.get("evidence_hash"),
        detected_at=event.get("created_at"),
        blockchain_status="✓ VERIFIED ON MST TESTNET",
        network=event.get("blockchain_network") or "MST Testnet",
        transaction_hash=event.get("blockchain_tx"),
        contract_address=event.get("contract_address"),
    )

    # Reuse the SAME evidence image from the original event.
    candidate_photo = (
        event.get("image_path")
        or event.get("body_image_path")
    )

    resolved_photo = None

    if candidate_photo:
        candidate_str = str(candidate_photo)

        # Absolute path
        if os.path.isfile(candidate_str):
            resolved_photo = candidate_str

        else:
            root_dir = Path(__file__).resolve().parent.parent
            file_name = Path(candidate_str).name

            # /static/captures/<filename>
            possible_path = root_dir / "static" / "captures" / file_name

            if possible_path.is_file():
                resolved_photo = str(possible_path)

            else:
                # Relative project path
                possible_path2 = root_dir / candidate_str.lstrip("/\\")
                if possible_path2.is_file():
                    resolved_photo = str(possible_path2)

    # VERIFIED message + SAME evidence photo
    if resolved_photo:
        sent = send_telegram_photo(
            resolved_photo,
            caption=message,
        )

        if sent:
            print(
                f"✅ Telegram blockchain verification sent with same evidence: "
                f"{resolved_photo}"
            )
            return True

    # Fallback if image is unavailable
    print("⚠️ Evidence image unavailable. Sending blockchain update as text.")
    return _send_telegram_message(message)