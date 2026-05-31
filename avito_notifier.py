#!/usr/bin/env python3
"""
Уведомления о новых сообщениях и заказах с Авито в Telegram.

Скрипт запускается по расписанию (GitHub Actions cron), опрашивает Avito API
и присылает в Telegram только то, что появилось с прошлой проверки.
Состояние того, что уже отправлено, хранится в state.json.

Переменные окружения (секреты):
    AVITO_CLIENT_ID       — client_id персонального приложения Авито
    AVITO_CLIENT_SECRET   — client_secret приложения Авито
    TELEGRAM_BOT_TOKEN    — токен бота от @BotFather
    TELEGRAM_CHAT_ID      — твой chat_id, куда слать уведомления

Необязательные:
    STATE_FILE            — путь к файлу состояния (по умолчанию state.json)
    AVITO_USER_ID         — числовой id аккаунта Авито (если не задан — берётся автоматически)
"""

import json
import os
import sys
import time
import html
from datetime import datetime, timezone

import requests

AVITO_API = "https://api.avito.ru"
TELEGRAM_API = "https://api.telegram.org"

# Сколько id хранить в памяти, чтобы не слать повторно. Хватает с большим запасом.
MAX_REMEMBERED = 1000
REQUEST_TIMEOUT = 30


# --------------------------------------------------------------------------- #
#   Состояние                                                                 #
# --------------------------------------------------------------------------- #
def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[warn] не удалось прочитать {path}: {e}; начинаю с чистого состояния")
            data = {}
    else:
        data = {}

    data.setdefault("initialized", False)
    data.setdefault("sent_message_ids", [])
    data.setdefault("sent_order_ids", [])
    return data


def save_state(path: str, state: dict) -> None:
    # держим списки в разумных пределах
    state["sent_message_ids"] = state["sent_message_ids"][-MAX_REMEMBERED:]
    state["sent_order_ids"] = state["sent_order_ids"][-MAX_REMEMBERED:]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
#   Avito                                                                     #
# --------------------------------------------------------------------------- #
def avito_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        f"{AVITO_API}/token/",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def avito_self_id(session: requests.Session) -> int:
    resp = session.get(f"{AVITO_API}/core/v1/accounts/self", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return int(resp.json()["id"])


def avito_chats(session: requests.Session, user_id: int) -> list:
    """Список чатов, отсортированных по времени обновления (свежие первыми)."""
    resp = session.get(
        f"{AVITO_API}/messenger/v2/accounts/{user_id}/chats",
        params={"limit": 100, "chat_types": "u2i,u2u"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("chats", [])


def avito_orders(session: requests.Session) -> list:
    """
    Заказы Авито Доставки.
    Доступ к Order API есть не у всех аккаунтов — поэтому ошибки тут не
    должны ломать работу с сообщениями. Возвращаем [] при любой проблеме.
    """
    try:
        resp = session.get(
            f"{AVITO_API}/order-management/1/orders",
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            print(f"[info] заказы: нет доступа к Order API ({resp.status_code}) — пропускаю")
            return []
        resp.raise_for_status()
        body = resp.json()
        return body.get("orders", []) if isinstance(body, dict) else []
    except requests.RequestException as e:
        print(f"[info] заказы недоступны, пропускаю: {e}")
        return []


# --------------------------------------------------------------------------- #
#   Telegram                                                                  #
# --------------------------------------------------------------------------- #
def telegram_send(bot_token: str, chat_id: str, text: str) -> None:
    resp = requests.post(
        f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=REQUEST_TIMEOUT,
    )
    if not resp.ok:
        print(f"[error] Telegram вернул {resp.status_code}: {resp.text}")
    resp.raise_for_status()


# --------------------------------------------------------------------------- #
#   Форматирование уведомлений                                                #
# --------------------------------------------------------------------------- #
def message_text(chat: dict) -> str:
    """Текст уведомления о новом сообщении."""
    last = chat.get("last_message", {}) or {}
    content = last.get("content", {}) or {}
    msg_type = last.get("type", "text")

    if msg_type == "text":
        body = content.get("text", "") or "(пустое сообщение)"
    elif msg_type == "image":
        body = "📷 Фотография"
    elif msg_type == "link":
        body = content.get("link", {}).get("text") or "🔗 Ссылка"
    elif msg_type == "item":
        body = "📦 Объявление"
    elif msg_type == "location":
        body = "📍 Геопозиция"
    elif msg_type == "call":
        body = "📞 Звонок"
    else:
        body = f"Сообщение ({msg_type})"

    # имя собеседника и название объявления
    author = "Покупатель"
    for user in chat.get("users", []):
        if user.get("id") and user.get("name"):
            author = user["name"]
            break

    title = ""
    ctx = chat.get("context", {}) or {}
    value = ctx.get("value", {}) or {}
    if value.get("title"):
        title = value["title"]
        price = value.get("price_string") or ""
        if price:
            title = f"{title} — {price}"

    chat_id = chat.get("id", "")
    link = f"https://www.avito.ru/profile/messenger/channel/{chat_id}" if chat_id else "https://www.avito.ru/profile/messenger"

    parts = ["💬 <b>Новое сообщение на Авито</b>"]
    parts.append(f"<b>От:</b> {html.escape(author)}")
    if title:
        parts.append(f"<b>Объявление:</b> {html.escape(title)}")
    parts.append("")
    parts.append(html.escape(body))
    parts.append("")
    parts.append(f'<a href="{link}">Открыть чат</a>')
    return "\n".join(parts)


ORDER_STATUS_RU = {
    "new": "Новый",
    "confirmed": "Подтверждён",
    "ready_to_ship": "Готов к отправке",
    "shipped": "Отправлен",
    "in_transit": "В пути",
    "delivered": "Доставлен",
    "received": "Получен",
    "completed": "Завершён",
    "cancelled": "Отменён",
    "canceled": "Отменён",
    "rejected": "Отклонён",
    "returned": "Возврат",
}


def _money(value) -> str:
    """7900 -> '7 900 ₽'."""
    try:
        return f"{int(value):,}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return str(value)


def order_text(order: dict) -> str:
    """Текст уведомления о новом заказе под реальную структуру Order API."""
    oid = order.get("id", "—")
    status_raw = order.get("status", "")
    status = ORDER_STATUS_RU.get(status_raw, status_raw)

    items = order.get("items", []) or []
    if items:
        title = items[0].get("title", "")
        count = items[0].get("count", 1)
        if count and count > 1:
            title = f"{title} ×{count}"
        if len(items) > 1:
            title = f"{title} и ещё {len(items) - 1}"
    else:
        title = ""

    prices = order.get("prices", {}) or {}
    price = prices.get("price")
    total = prices.get("total")

    delivery = order.get("delivery", {}) or {}
    service = delivery.get("serviceName", "")
    track = delivery.get("trackingNumber") or delivery.get("dispatchNumber") or ""

    ship_till = (order.get("schedules", {}) or {}).get("shipTill", "")

    parts = ["🛒 <b>Новый заказ на Авито</b>", f"<b>Заказ №:</b> {html.escape(str(oid))}"]
    if title:
        parts.append(f"<b>Товар:</b> {html.escape(str(title))}")
    if price is not None:
        line = f"<b>Цена:</b> {_money(price)}"
        if total is not None and total != price:
            line += f"  (к получению {_money(total)})"
        parts.append(line)
    if service:
        parts.append(f"<b>Доставка:</b> {html.escape(str(service))}")
    if track:
        parts.append(f"<b>Трек:</b> {html.escape(str(track))}")
    if status:
        parts.append(f"<b>Статус:</b> {html.escape(str(status))}")
    if ship_till:
        parts.append(f"<b>Отгрузить до:</b> {html.escape(str(ship_till)[:10])}")
    parts.append("")
    parts.append('<a href="https://www.avito.ru/profile/orders">Открыть заказы</a>')
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
#   Основная логика                                                           #
# --------------------------------------------------------------------------- #
def env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "").strip()
    if required and not val:
        print(f"[fatal] не задана переменная окружения {name}")
        sys.exit(1)
    return val


def main() -> int:
    client_id = env("AVITO_CLIENT_ID")
    client_secret = env("AVITO_CLIENT_SECRET")
    bot_token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    state_file = os.environ.get("STATE_FILE", "state.json")

    state = load_state(state_file)
    first_run = not state["initialized"]

    # 1. Авторизация в Авито
    token = avito_token(client_id, client_secret)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}"})

    # 2. user_id
    user_id = os.environ.get("AVITO_USER_ID", "").strip()
    user_id = int(user_id) if user_id else avito_self_id(session)

    sent_messages = set(state["sent_message_ids"])
    sent_orders = set(state["sent_order_ids"])

    new_message_notifications = []
    new_order_notifications = []

    # 3. Сообщения
    try:
        for chat in avito_chats(session, user_id):
            last = chat.get("last_message", {}) or {}
            msg_id = last.get("id")
            direction = last.get("direction")  # "in" — входящее от клиента
            if not msg_id or direction != "in":
                continue
            if msg_id in sent_messages:
                continue
            sent_messages.add(msg_id)
            state["sent_message_ids"].append(msg_id)
            if not first_run:
                new_message_notifications.append(message_text(chat))
    except requests.RequestException as e:
        print(f"[error] не удалось получить чаты: {e}")

    # 4. Заказы
    for order in avito_orders(session):
        oid = order.get("id") or order.get("order_id") or order.get("number")
        if oid is None:
            continue
        oid = str(oid)
        if oid in sent_orders:
            continue
        sent_orders.add(oid)
        state["sent_order_ids"].append(oid)
        if not first_run:
            new_order_notifications.append(order_text(order))

    # 5. Отправка
    if first_run:
        state["initialized"] = True
        save_state(state_file, state)
        telegram_send(
            bot_token,
            chat_id,
            "✅ <b>Бот уведомлений Авито запущен</b>\n"
            "Теперь новые сообщения и заказы будут приходить сюда.",
        )
        print(f"[ok] первый запуск: запомнено {len(sent_messages)} сообщений, "
              f"{len(sent_orders)} заказов; уведомления — со следующего цикла")
        return 0

    sent_count = 0
    for text in new_message_notifications + new_order_notifications:
        telegram_send(bot_token, chat_id, text)
        sent_count += 1
        time.sleep(0.4)  # не упираться в лимит Telegram

    save_state(state_file, state)
    print(f"[ok] {datetime.now(timezone.utc).isoformat()} — отправлено уведомлений: {sent_count} "
          f"(сообщений: {len(new_message_notifications)}, заказов: {len(new_order_notifications)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
