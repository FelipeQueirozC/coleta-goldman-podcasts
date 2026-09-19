"""Email and Telegram delivery adapters."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

import formatting


SENDER_PREFIXES = {
    "the_markets": "gs.themarkets",
    "exchanges": "gs.exchanges",
    "views_from_floor": "gs.viewsfromfloor",
}


@dataclass(frozen=True)
class DeliveryConfig:
    resend_api_key: str
    resend_from_domain: str
    resend_to: list[str]
    telegram_bot_token: str
    telegram_delivery_chat_id: str
    telegram_error_chat_id: str


def build_email_payload(episode, summary: str, routing, config: DeliveryConfig, attachment: Path) -> dict:
    prefix = SENDER_PREFIXES[episode.source_id]
    return {
        "from": f"{prefix}@{config.resend_from_domain.strip('@')}",
        "to": config.resend_to,
        "subject": formatting.build_email_subject(episode),
        "text": formatting.build_email_text(episode, summary, routing),
        "html": formatting.build_email_html(episode, summary, routing),
        "attachments": [
            {
                "filename": attachment.name,
                "content": base64.b64encode(attachment.read_bytes()).decode("ascii"),
            }
        ],
    }


def send_email(episode, summary: str, routing, config: DeliveryConfig, attachment: Path) -> str:
    import resend

    resend.api_key = config.resend_api_key
    response = resend.Emails.send(
        build_email_payload(episode, summary, routing, config, attachment)
    )
    email_id = (
        str(response.get("id") or "")
        if isinstance(response, dict)
        else str(getattr(response, "id", "") or "")
    )
    if not email_id:
        raise RuntimeError("Resend did not return an email id")
    return email_id


def _telegram_response(response: requests.Response, operation: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram {operation} failed ({exc.__class__.__name__})") from exc
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {operation} failed: {body}")
    return body["result"]


def send_telegram_message(episode, summary: str, routing, config: DeliveryConfig) -> str:
    response = requests.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        json={
            "chat_id": config.telegram_delivery_chat_id,
            "text": formatting.build_telegram_message(episode, summary, routing)[:4096],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=60,
    )
    return str(_telegram_response(response, "sendMessage")["message_id"])


def send_telegram_document(episode, routing, config: DeliveryConfig, attachment: Path) -> str:
    with attachment.open("rb") as handle:
        response = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/sendDocument",
            data={
                "chat_id": config.telegram_delivery_chat_id,
                "caption": formatting.build_telegram_caption(episode, routing)[:1024],
            },
            files={"document": (attachment.name, handle, "text/html")},
            timeout=180,
        )
    return str(_telegram_response(response, "sendDocument")["message_id"])


def send_error_message(config: DeliveryConfig, text: str) -> str:
    response = requests.post(
        f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage",
        json={
            "chat_id": config.telegram_error_chat_id,
            "text": text[:4096],
            "disable_web_page_preview": True,
        },
        timeout=60,
    )
    return str(_telegram_response(response, "error sendMessage")["message_id"])
