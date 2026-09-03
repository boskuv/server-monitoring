import httpx


def send_message(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30.0,
    )
    if not response.is_success:
        raise RuntimeError(
            f"Telegram API error {response.status_code}: {response.text}"
        )


def send_document(
    token: str,
    chat_id: str,
    filename: str,
    content: bytes,
    *,
    caption: str | None = None,
    content_type: str = "text/html",
) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    data: dict[str, str] = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption

    response = httpx.post(
        url,
        data=data,
        files={"document": (filename, content, content_type)},
        timeout=60.0,
    )
    if not response.is_success:
        raise RuntimeError(
            f"Telegram API error {response.status_code}: {response.text}"
        )
