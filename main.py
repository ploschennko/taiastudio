import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import Optional

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from aiocryptopay import AioCryptoPay, Networks
from aiocryptopay.models.update import Update as CryptoUpdate


# =========================
# Config
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty")

# Crypto Pay API token (CryptoBot -> Crypto Pay -> My Apps -> API Token)
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN", "").strip()

# Безопасный секретный путь для Crypto Pay вебхука (рекомендуется)
CRYPTO_WEBHOOK_PATH = os.getenv("CRYPTO_WEBHOOK_PATH", "/cryptopay_webhook").strip()

# Вариант оплаты:
#   STARS_ONLY  - только Telegram Stars (комплаенс и проще)
#   STARS_PLUS_CRYPTO - Stars + Crypto Pay
PAYMENTS_MODE = os.getenv("PAYMENTS_MODE", "STARS_ONLY").strip().upper()

# Куда вести на менеджера
MANAGER_URL = os.getenv("MANAGER_URL", "https://t.me/taiastudio").strip()

# Материалы (можно заменить на вашу платформу/Notion/Google Drive и т.д.)
BASE_ACCESS_TEXT = os.getenv("BASE_ACCESS_TEXT", "Доступ буде виданий менеджером після підтвердження оплати.")
PRO_ACCESS_TEXT = os.getenv("PRO_ACCESS_TEXT", "Доступ буде виданий менеджером + додамо в чат підтримки.")
MENTOR_ACCESS_TEXT = os.getenv("MENTOR_ACCESS_TEXT", "Напиши менеджеру для старту під ключ.")

# Stars amounts (фиксируем)
BASE_STARS = int(os.getenv("BASE_STARS", "399"))
PRO_STARS = int(os.getenv("PRO_STARS", "799"))
MENTOR_STARS = int(os.getenv("MENTOR_STARS", "2000"))

# USD якорь в тексте (маркетинг)
BASE_USD = os.getenv("BASE_USD", "39")
PRO_USD = os.getenv("PRO_USD", "79")
MENTOR_USD = os.getenv("MENTOR_USD", "200")

# Render порт
PORT = int(os.getenv("PORT", "10000"))


# =========================
# Bot / Dispatcher
# =========================

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# =========================
# Crypto Pay init (optional)
# =========================

crypto: Optional[AioCryptoPay] = None
if PAYMENTS_MODE == "STARS_PLUS_CRYPTO":
    if not CRYPTO_TOKEN:
        raise RuntimeError("CRYPTO_TOKEN is required for STARS_PLUS_CRYPTO mode")
    crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)


# =========================
# Copy (UA, строгий стиль, HTML)
# =========================

TEXT_START = "Це система, не курс. ⚙️"

TEXT_NOT_PROMPTS = (
    "<b>Що варто знати:</b> це не про промпти. "
    "Це про <b>архітектуру</b> та <b>автоматизацію</b>."
)

TEXT_STRUCTURE = (
    "Люди не заробляють через відсутність структури. "
    "<b>Хаос не масштабується.</b>"
)

TEXT_CHOOSE = "<b>Обери свій формат взаємодії</b> 👇"

TEXT_BASE = (
    "🥉 <b>BASE — «сам»</b>\n"
    "Для тих, хто хоче розібратись і зробити сам.\n\n"
    "<b>Всередині:</b>\n"
    "• доступ до системи\n"
    "• всі матеріали\n"
    "• структура\n\n"
    "❌ <i>Без підтримки / перевірки / контролю.</i>\n"
    "👉 <b>Позиція:</b> сам відповідаєш за результат."
)

TEXT_PRO = (
    "🥈 <b>PRO — «з підтримкою»</b> 🔥\n"
    "Для тих, хто хоче швидше і з фідбеком.\n\n"
    "<b>Всередині:</b>\n"
    "• все з BASE\n"
    "• закритий чат\n"
    "• мій персональний фідбек\n"
    "• відповіді на питання\n\n"
    "✅ <i>Ти не даєш злитись.</i>\n"
    "👉 <b>Позиція:</b> ти робиш, я направляю."
)

TEXT_MENTOR = (
    "🥇 <b>MENTORSHIP — «під ключ»</b>\n"
    "Для тих, хто хоче максимум результату і швидко.\n\n"
    "<b>Всередині:</b>\n"
    "• все з PRO\n"
    "• персональна робота\n"
    "• розбори та контроль\n"
    "• допомога з реалізацією\n\n"
    "👉 <b>Позиція:</b> ми доводимо тебе до результату."
)


# =========================
# Callback data (<=64 bytes)
# =========================

CB_CONTINUE = "flow:continue"
CB_LEARN = "flow:learn"
CB_ORDER = "flow:order"

CB_BUY_BASE_STARS = "buy:base:stars"
CB_BUY_PRO_STARS = "buy:pro:stars"
CB_BUY_MENTOR_STARS = "buy:mentor:stars"

CB_BUY_BASE_CRYPTO = "buy:base:crypto"
CB_BUY_PRO_CRYPTO = "buy:pro:crypto"
CB_BUY_MENTOR_CRYPTO = "buy:mentor:crypto"


# =========================
# Keyboards
# =========================

def kb_continue() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👉 Продовжити", callback_data=CB_CONTINUE)]
        ]
    )

def kb_choose() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Навчитися і зробити самому", callback_data=CB_LEARN)],
            [InlineKeyboardButton(text="🚀 Замовити під себе", callback_data=CB_ORDER)],
        ]
    )

def kb_pay_base() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ Оплатити BASE ({BASE_STARS} Stars)", callback_data=CB_BUY_BASE_STARS)]
    ]
    if PAYMENTS_MODE == "STARS_PLUS_CRYPTO":
        rows.append([InlineKeyboardButton(text=f"💎 Оплатити USDT (${BASE_USD})", callback_data=CB_BUY_BASE_CRYPTO)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pay_pro() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ Почати з підтримкою ({PRO_STARS} Stars)", callback_data=CB_BUY_PRO_STARS)]
    ]
    if PAYMENTS_MODE == "STARS_PLUS_CRYPTO":
        rows.append([InlineKeyboardButton(text=f"💎 Оплатити USDT (${PRO_USD})", callback_data=CB_BUY_PRO_CRYPTO)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pay_mentor() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"⭐ Замовити систему ({MENTOR_STARS} Stars)", callback_data=CB_BUY_MENTOR_STARS)],
        [InlineKeyboardButton(text="💬 Написати менеджеру", url=MANAGER_URL)],
    ]
    if PAYMENTS_MODE == "STARS_PLUS_CRYPTO":
        rows.insert(1, [InlineKeyboardButton(text=f"💎 Оплатити USDT (${MENTOR_USD})", callback_data=CB_BUY_MENTOR_CRYPTO)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# =========================
# Helpers
# =========================

async def pause_typing(chat_id: int, seconds: float = 2.0) -> None:
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await asyncio.sleep(seconds)

def stars_invoice(message: Message, title: str, description: str, amount_stars: int, payload: str):
    # IMPORTANT:
    # - currency must be XTR
    # - prices must contain exactly one item
    # - provider_token: aiogram docs allow empty string for Stars
    return message.answer_invoice(
        title=title,
        description=description,
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount_stars)],
        payload=payload,
    )


# =========================
# Funnel handlers
# =========================

@router.message(CommandStart())
async def on_start(message: Message) -> None:
    await message.answer(TEXT_START, reply_markup=kb_continue())

@router.callback_query(F.data == CB_CONTINUE)
async def on_continue(cb: CallbackQuery) -> None:
    await cb.answer()
    # Убираем кнопку, чтобы не жали 2 раза
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    chat_id = cb.message.chat.id

    await pause_typing(chat_id, 2)
    await cb.message.answer(TEXT_NOT_PROMPTS)

    await pause_typing(chat_id, 2)
    await cb.message.answer(TEXT_STRUCTURE)

    await pause_typing(chat_id, 2)
    await cb.message.answer(TEXT_CHOOSE, reply_markup=kb_choose())

@router.callback_query(F.data == CB_LEARN)
async def on_learn(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(TEXT_BASE, reply_markup=kb_pay_base())
    # Без действия пользователя: сразу PRO
    await asyncio.sleep(0.6)
    await cb.message.answer(TEXT_PRO, reply_markup=kb_pay_pro())

@router.callback_query(F.data == CB_ORDER)
async def on_order(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(TEXT_MENTOR, reply_markup=kb_pay_mentor())


# =========================
# Telegram Stars payments
# =========================

@router.callback_query(F.data.in_({CB_BUY_BASE_STARS, CB_BUY_PRO_STARS, CB_BUY_MENTOR_STARS}))
async def on_buy_stars(cb: CallbackQuery) -> None:
    await cb.answer()
    msg = cb.message

    user_id = cb.from_user.id

    if cb.data == CB_BUY_BASE_STARS:
        await stars_invoice(msg, "BASE", "Доступ до системи та матеріалів. Без підтримки.", BASE_STARS, f"base:{user_id}")
    elif cb.data == CB_BUY_PRO_STARS:
        await stars_invoice(msg, "PRO", "BASE + чат та персональний фідбек.", PRO_STARS, f"pro:{user_id}")
    else:
        await stars_invoice(msg, "MENTORSHIP", "Під ключ: контроль, реалізація, результат.", MENTOR_STARS, f"mentor:{user_id}")

@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    # Нужно ответить в течение 10 секунд, иначе платеж отменится.
    await query.answer(ok=True)

@router.message(F.successful_payment)
async def on_success_payment(message: Message) -> None:
    payload = message.successful_payment.invoice_payload  # base:USERID | pro:USERID | mentor:USERID
    product = payload.split(":")[0]

    if product == "base":
        await message.answer(f"Оплата успішна ✅\n\n{BASE_ACCESS_TEXT}")
    elif product == "pro":
        await message.answer(f"Оплата успішна ✅\n\n{PRO_ACCESS_TEXT}\n\nМенеджер: {MANAGER_URL}")
    else:
        await message.answer(f"Оплата успішна ✅\n\n{MENTOR_ACCESS_TEXT}\n\nМенеджер: {MANAGER_URL}")


# =========================
# Crypto Pay payments (optional)
# =========================

async def create_crypto_invoice(user_id: int, plan: str, amount_usdt: float) -> str:
    assert crypto is not None
    inv = await crypto.create_invoice(
        asset="USDT",
        amount=amount_usdt,
        description=f"Plan {plan}",
        payload=f"{user_id}:{plan}",
    )
    return inv.bot_invoice_url  # лучше использовать bot_invoice_url, pay_url в API deprecated
    # Crypto Pay docs mention pay_url deprecation in favor of bot_invoice_url in Invoice changes.


@router.callback_query(F.data.in_({CB_BUY_BASE_CRYPTO, CB_BUY_PRO_CRYPTO, CB_BUY_MENTOR_CRYPTO}))
async def on_buy_crypto(cb: CallbackQuery) -> None:
    if PAYMENTS_MODE != "STARS_PLUS_CRYPTO":
        await cb.answer("Крипто-оплата вимкнена.", show_alert=True)
        return
    await cb.answer()

    user_id = cb.from_user.id

    if cb.data == CB_BUY_BASE_CRYPTO:
        url = await create_crypto_invoice(user_id, "BASE", float(BASE_USD))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатити в CryptoBot", url=url)]])
        await cb.message.answer("Інвойс створено. Оплати та дочекайся підтвердження ✅", reply_markup=kb)

    elif cb.data == CB_BUY_PRO_CRYPTO:
        url = await create_crypto_invoice(user_id, "PRO", float(PRO_USD))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатити в CryptoBot", url=url)]])
        await cb.message.answer("Інвойс створено. Оплати та дочекайся підтвердження ✅", reply_markup=kb)

    else:
        url = await create_crypto_invoice(user_id, "MENTORSHIP", float(MENTOR_USD))
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатити в CryptoBot", url=url)]])
        await cb.message.answer("Інвойс створено. Оплати та дочекайся підтвердження ✅", reply_markup=kb)


def verify_cryptopay_signature(raw_body: bytes, signature_header: str, token: str) -> bool:
    """
    Crypto Pay API: signature = HMAC-SHA-256(body, secret), secret = SHA256(app_token)
    Comparison must be exact. If header missing -> reject.
    """
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    calc = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, signature_header)


async def cryptopay_webhook_handler(request: web.Request) -> web.Response:
    """
    Принимаем webhook от Crypto Pay API.
    ВАЖНО:
    - проверяем crypto-pay-api-signature
    - отвечаем быстро (200 OK), обработка должна быть идемпотентной
    """
    if PAYMENTS_MODE != "STARS_PLUS_CRYPTO":
        return web.Response(status=404, text="disabled")

    assert CRYPTO_TOKEN
    assert crypto is not None

    raw = await request.read()
    sig = request.headers.get("crypto-pay-api-signature", "")
    if not sig or not verify_cryptopay_signature(raw, sig, CRYPTO_TOKEN):
        return web.Response(status=401, text="bad signature")

    # Обрабатываем обновление через aiocryptopay парсер
    try:
        data = json.loads(raw.decode("utf-8"))
        upd = CryptoUpdate.model_validate(data)  # pydantic v2 style in newer versions
    except Exception:
        logging.exception("Failed to parse Crypto Pay update")
        return web.Response(status=400, text="bad payload")

    # invoice_paid
    if upd.update_type == "invoice_paid":
        payload = getattr(upd.payload, "payload", None) or ""
        # payload format: "user_id:PLAN"
        try:
            user_str, plan = payload.split(":")
            user_id = int(user_str)
        except Exception:
            logging.error("Bad payload format: %r", payload)
            return web.Response(status=200, text="ok")

        if plan == "BASE":
            await bot.send_message(user_id, f"Оплата успішна ⚡️\n\n{BASE_ACCESS_TEXT}")
        elif plan == "PRO":
            await bot.send_message(user_id, f"Оплата успішна 🔥\n\n{PRO_ACCESS_TEXT}\n\nМенеджер: {MANAGER_URL}")
        else:
            await bot.send_message(user_id, f"Оплата успішна 🚀\n\n{MENTOR_ACCESS_TEXT}\n\nМенеджер: {MANAGER_URL}")

    return web.Response(status=200, text="ok")


# =========================
# App bootstrap
# =========================

async def on_startup(app: web.Application) -> None:
    # Важно: polling и webhook взаимоисключающие. Здесь мы оставляем polling как MVP.
    # Если вы решите перейти на Telegram webhook — нужно выключить polling.
    await bot.delete_webhook(drop_pending_updates=True)

async def on_cleanup(app: web.Application) -> None:
    if crypto is not None:
        await crypto.close()
    await bot.session.close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Healthcheck route (удобно для Render)
    async def health(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", health)

    # Crypto Pay webhook route
    app.router.add_post(CRYPTO_WEBHOOK_PATH, cryptopay_webhook_handler)
    return app


async def run() -> None:
    logging.basicConfig(level=logging.INFO)

    # Одновременно:
    # 1) HTTP сервер (Render требует открытый порт для Web Service)
    # 2) polling бота (MVP)
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run())
