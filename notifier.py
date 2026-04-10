"""Google Chat webhook notifier."""
from __future__ import annotations

import requests


def send_chat_message(webhook_url: str, text: str) -> None:
    """Send plain text to a Google Chat space."""
    resp = requests.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def send_chat_alert(webhook_url: str, title: str, subtitle: str,
                    rows: list[tuple[str, str]], severity: str = "WARNING") -> None:
    """Send a card-formatted alert. `rows` is a list of (key, value) pairs."""
    color_map = {"WARNING": "⚠️", "CRITICAL": "🚨", "INFO": "ℹ️"}
    icon = color_map.get(severity, "📊")

    widgets = [{"decoratedText": {"topLabel": k, "text": v}} for k, v in rows]

    card = {
        "cardsV2": [{
            "cardId": "askmxp-alert",
            "card": {
                "header": {
                    "title": f"{icon} {title}",
                    "subtitle": subtitle,
                },
                "sections": [{"widgets": widgets}],
            },
        }]
    }
    resp = requests.post(webhook_url, json=card, timeout=15)
    resp.raise_for_status()


def send_analysis_share(webhook_url: str, event_name: str, summary: str) -> None:
    """Manual share from Streamlit sidebar: push an analysis summary to Chat."""
    text = f"*📊 AskMXP 分析分享 — {event_name}*\n\n{summary}"
    send_chat_message(webhook_url, text)
