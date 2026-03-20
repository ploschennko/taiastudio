import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from aiocryptopay import AioCryptoPay, Networks
from aiocryptopay.models.update import Update as CryptoUpdate


# =========================
# Config
# =========================

class PayProvider(str, Enum):
    STARS = "STARS"          # основной, комплаенс внутри Telegram
    CRYPTOPAY = "CRYPTOPAY"  # опционально (учитывайте policy Telegram)
    LINK = "LINK"            # внешняя ссылка (учитывайте policy Telegram)


@dataclass(frozen=True)
class Config:
    bot_token: str

    # Telegram webhook
    tg_webhook_path: str
    tg_webhook_secret: str

    # Render / public base url
    public_base_url: str
    port: int

    # Funnel payment behavior
    pay_provider: PayProvider

    # Stars amounts (XTR)
    base_stars: int
    pro_stars: int
    mentor_stars: int

    # Link fallback
    base_pay_url: Optional[str]
    pro_pay_url: Optional[str]
    mentor_pay_url: Optional[str]

    # Manager / support
    manager_url: str
    paysupport_text: str

    # Crypto Pay
    crypto_token: Optional[str]
    crypto_webhook_path: str


def must_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def get_public_base_url() -> str:
    # Render предоставляет RENDER_EXTERNAL_URL для web service (https://<name>.onrender.com)
    # Если есть кастомный домен — задайте PUBLIC_BASE_URL вручную.
    manual = os.getenv("PUBLIC_BASE_URL", "").strip()
    if manual:
        return manual.rstrip("/")
    auto = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if auto:
        return auto.rstrip("/")
    raise RuntimeError("PUBLIC_BASE_URL or RENDER_EXTERNAL_URL must be set for webhook mode.")


CFG = Config(
    bot_token=must_env("BOT_TOKEN"),
    tg_webhook_path=os.getenv("TG_WEBHOOK_PATH", "/tg/webhook").strip(),
    tg_webhook_secret=must_env("TG_WEBHOOK_SECRET"),
    public_base_url=get_public_base_url(),
    port=int(os.getenv("PORT", "10000")),
    pay_provider=PayProvider(os.getenv("PAY_PROVIDER", "STARS").strip().upper()),
    base_stars=int(os.getenv("BASE_STARS", "399")),
    pro_stars=int(os.getenv("PRO_STARS", "799")),
    mentor_stars=int(os.getenv("MENTOR_STARS", "2000")),
    base_pay_url=os.getenv("BASE_PAY_URL"),
    pro_pay_url=os.getenv("PRO_PAY_URL"),
    mentor_pay_url=os.getenv("MENTOR_PAY_URL"),
    manager_url=os.getenv("MANAGER_URL", "https://t.me/taiastudio").strip(),
    paysupport_text=os.getenv(
        "PAYSUPPORT_TEXT",
        "Питання по оплаті або доступу: @velvettaya / @taiastudio",
    ).strip(),
    crypto_token=os.getenv("CRYPTO_TOKEN"),
    crypto_webhook_path=os.getenv("CRYPTO_WEBHOOK_PATH", "/cryptopay/webhook").strip(),
)

# Normalize paths
if not CFG.tg_webhook_path.startswith("/"):
    raise RuntimeError("TG_WEBHOOK_PATH must start with /")
if not CFG.crypto_webhook_path.startswith("/"):
    raise RuntimeError("CRYPTO_WEBHOOK_PATH must start with /")


# =========================
# Bot / Dispatcher
# =========================

bot = Bot(token=CFG.bot_token, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)


# =========================
# Funnel copy (UA + HTML) - EXACT visible text as required
# =========================

TEXT_START = "<b>Це система, не курс. ⚙️</b>"

TEXT_STEP3 = "Що варто знати: це не про промпти. Це про <b>архітектуру</b> та <b>автоматизацію</b>."
TEXT_STEP4 = "Люди не заробляють через відсутність структури. <b>Хаос не масштабується.</b>"
TEXT_STEP5 = "<b>Обери свій формат взаємодії 👇</b>"

TEXT_BASE = "🥉 <b>Формат BASE.</b> Доступ до системи та матеріалів. <i>Без підтримки.</i> Сам відповідаєш за результат."
TEXT_PRO = "🥈 <b>Формат PRO 🔥.</b> Все з BASE + закритий чат та мій особистий фідбек. Ти робиш, я направляю."
TEXT_MENTOR = "🥇 <b>Формат MENTORSHIP.</b> Ми доводимо тебе до результату під ключ. Боти, AI-системи, сайти."


# =========================
# Callback data (<= 64 bytes)
# =========================

CB_CONTINUE = "f:continue"
CB_LEARN = "f:learn"
CB_ORDER = "f:order"
CB_PAY_BASE = "pay:base"
CB_PAY_PRO = "pay:pro"
CB_PAY_MENTOR = "pay:mentor"


# =========================
# Keyboards
# =========================

def kb_continue() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="👉 Продовжити", callback_data=CB_CONTINUE)]]
    )

def kb_choose() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛠 Навчитися і зробити самому", callback_data=CB_LEARN)],
            [InlineKeyboardButton(text="🚀 Замовити під себе", callback_data=CB_ORDER)],
        ]
    )

def kb_manager() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="💬 Написати менеджеру", url=CFG.manager_url)]]
    )

def pay_button(label: str, callback_data: str, url: Optional[str]) -> InlineKeyboardButton:
    # В LINK режиме — URL-кнопка (если ссылка задана)
    if CFG.pay_provider == PayProvider.LINK and url:
        return InlineKeyboardButton(text=label, url=url)
    # В STARS/CRYPTOPAY режимах — callback (бот сам создаст invoice/ссылку)
    return InlineKeyboardButton(text=label, callback_data=callback_data)

def kb_pay_base() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[pay_button("💳 Оплатити BASE ($39)", CB_PAY_BASE, CFG.base_pay_url)]]
    )

def kb_pay_pro() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[pay_button("💳 Почати з підтримкою ($79)", CB_PAY_PRO, CFG.pro_pay_url)]]
    )

def kb_pay_mentor() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [pay_button("💳 Замовити систему ($200)", CB_PAY_MENTOR, CFG.mentor_pay_url)],
            [InlineKeyboardButton(text="💬 Написати менеджеру", url=CFG.manager_url)],
        ]
    )


# =========================
# Utility: typing pauses
# =========================

async def typing_pause(chat_id: int, seconds: float = 2.0) -> None:
    await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    await asyncio.sleep(seconds)


# =========================
# /start and funnel handlers
# =========================

@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(TEXT_START, reply_markup=kb_continue())

@router.callback_query(F.data == CB_CONTINUE)
async def step_continue(cb: CallbackQuery) -> None:
    await cb.answer()
    # убираем кнопку, чтобы не нажимали дважды
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    chat_id = cb.message.chat.id

    await typing_pause(chat_id, 2)
    await cb.message.answer(TEXT_STEP3)

    await typing_pause(chat_id, 2)
    await cb.message.answer(TEXT_STEP4)

    await typing_pause(chat_id, 2)
    await cb.message.answer(TEXT_STEP5, reply_markup=kb_choose())

@router.callback_query(F.data == CB_LEARN)
async def branch_learn(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(TEXT_BASE, reply_markup=kb_pay_base())
    await asyncio.sleep(0.6)
    await cb.message.answer(TEXT_PRO, reply_markup=kb_pay_pro())

@router.callback_query(F.data == CB_ORDER)
async def branch_order(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(TEXT_MENTOR, reply_markup=kb_pay_mentor())

@router.message(Command("paysupport"))
async def paysupport(message: Message) -> None:
    await message.answer(CFG.paysupport_text, reply_markup=kb_manager())


# =========================
# Telegram Stars payments
# =========================

async def send_stars_invoice(message: Message, title: str, description: str, amount_stars: int, payload: str) -> None:
    await message.answer_invoice(
        title=title,
        description=description,
        prices=[LabeledPrice(label=title, amount=amount_stars)],  # must be exactly one item for Stars
        payload=payload,  # keep <=128 bytes
        currency="XTR",
        provider_token="",  # Stars допускают пустой provider_token
    )

@router.callback_query(F.data.in_({CB_PAY_BASE, CB_PAY_PRO, CB_PAY_MENTOR}))
async def on_pay_clicked(cb: CallbackQuery) -> None:
    await cb.answer()
    msg = cb.message
    user_id = cb.from_user.id

    if CFG.pay_provider == PayProvider.LINK:
        # Если ссылки не заданы — уводим на менеджера.
        await msg.answer("Оплата за посиланням зараз не налаштована. Напиши менеджеру.", reply_markup=kb_manager())
        return

    if CFG.pay_provider == PayProvider.STARS:
        if cb.data == CB_PAY_BASE:
            await send_stars_invoice(msg, "BASE", "Доступ до системи та матеріалів. Без підтримки.", CFG.base_stars, f"base:{user_id}")
        elif cb.data == CB_PAY_PRO:
            await send_stars_invoice(msg, "PRO", "Все з BASE + чат та фідбек.", CFG.pro_stars, f"pro:{user_id}")
        else:
            await send_stars_invoice(msg, "MENTORSHIP", "Під ключ: боти, AI‑системи, сайти.", CFG.mentor_stars, f"mentor:{user_id}")
        return

    # CFG.pay_provider == CRYPTOPAY
    if not CFG.crypto_token:
        await msg.answer("Crypto Pay не налаштований. Напиши менеджеру.", reply_markup=kb_manager())
        return

    # Создаём Crypto Pay invoice и отдаём bot_invoice_url
    crypto = AioCryptoPay(token=CFG.crypto_token, network=Networks.MAIN_NET)
    try:
        if cb.data == CB_PAY_BASE:
            inv = await crypto.create_invoice(asset="USDT", amount=39, description="BASE", payload=f"{user_id}:BASE")
        elif cb.data == CB_PAY_PRO:
            inv = await crypto.create_invoice(asset="USDT", amount=79, description="PRO", payload=f"{user_id}:PRO")
        else:
            inv = await crypto.create_invoice(asset="USDT", amount=200, description="MENTORSHIP", payload=f"{user_id}:MENTOR")
        url = inv.bot_invoice_url
    finally:
        await crypto.close()

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатити в CryptoBot", url=url)]])
    await msg.answer("Інвойс створено. Оплати та дочекайся підтвердження ✅", reply_markup=kb)

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery) -> None:
    await pre.answer(ok=True)

@router.message(F.successful_payment)
async def on_success_payment(message: Message) -> None:
    p = message.successful_payment.invoice_payload  # base:<id> | pro:<id> | mentor:<id>
    product = p.split(":")[0]
    if product == "base":
        await message.answer("Оплата успішна ✅\nДоступ буде виданий менеджером.", reply_markup=kb_manager())
    elif product == "pro":
        await message.answer("Оплата успішна ✅\nДалі: менеджер додасть у чат та дасть доступ.", reply_markup=kb_manager())
    else:
        await message.answer("Оплата успішна ✅\nДалі: менеджер стартує MENTORSHIP.", reply_markup=kb_manager())


# =========================
# Crypto Pay webhook security (optional, provider=CRYPTOPAY)
# =========================

def verify_cryptopay_signature(raw_body: bytes, signature_header: str, token: str) -> bool:
    """
    Crypto Pay: crypto-pay-api-signature == hex(HMAC-SHA256(raw_body, secret)),
    secret = SHA256(app_token).
    """
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    calc = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, signature_header)

async def cryptopay_webhook(request: web.Request) -> web.Response:
    if CFG.pay_provider != PayProvider.CRYPTOPAY:
        return web.Response(status=404, text="disabled")

    if not CFG.crypto_token:
        return web.Response(status=500, text="missing CRYPTO_TOKEN")

    raw = await request.read()
    sig = request.headers.get("crypto-pay-api-signature", "")
    if not sig or not verify_cryptopay_signature(raw, sig, CFG.crypto_token):
        return web.Response(status=401, text="bad signature")

    try:
        data = json.loads(raw.decode("utf-8"))
        upd = CryptoUpdate.model_validate(data)
    except Exception:
        logging.exception("Bad Crypto Pay payload")
        return web.Response(status=400, text="bad payload")

    # invoice_paid
    if upd.update_type == "invoice_paid":
        # payload в invoice может быть до 4kb; мы используем формат "<user_id>:<PLAN>"
        inv = upd.payload
        payload = getattr(inv, "payload", "") or ""
        invoice_id = getattr(inv, "invoice_id", None)
        try:
            user_str, plan = payload.split(":")
            user_id = int(user_str)
        except Exception:
            logging.warning("Bad payload format: %r", payload)
            return web.Response(text="ok")

        # В продакшне добавьте идемпотентность по invoice_id (хранилище/Redis/Postgres)
        if invoice_id is not None:
            logging.info("Paid invoice_id=%s plan=%s user=%s", invoice_id, plan, user_id)

        if plan == "BASE":
            await bot.send_message(user_id, "Оплата успішна ⚡️\nДоступ буде виданий менеджером.", reply_markup=kb_manager())
        elif plan == "PRO":
            await bot.send_message(user_id, "Оплата успішна 🔥\nДалі: менеджер додасть у чат та дасть доступ.", reply_markup=kb_manager())
        else:
            await bot.send_message(user_id, "Оплата успішна 🚀\nДалі: менеджер стартує MENTORSHIP.", reply_markup=kb_manager())

    return web.Response(text="ok")


# =========================
# App bootstrap (aiohttp)
# =========================

async def on_startup(app: web.Application) -> None:
    webhook_url = f"{CFG.public_base_url}{CFG.tg_webhook_path}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=CFG.tg_webhook_secret,
        drop_pending_updates=True,
    )
    logging.info("Webhook set: %s", webhook_url)

async def on_shutdown(app: web.Application) -> None:
    await bot.session.close()

def create_app() -> web.Application:
    app = web.Application()

    # health endpoint
    async def health(_req: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/health", health)

    # Telegram webhook via aiogram request handler
    request_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=CFG.tg_webhook_secret,
        handle_in_background=True,  # immediate 200 OK, work in background
    )
    request_handler.register(app, path=CFG.tg_webhook_path)

    # Crypto Pay webhook endpoint (optional)
    app.router.add_post(CFG.crypto_webhook_path, cryptopay_webhook)

    # Mount dispatcher startup/shutdown hooks to aiohttp app
    dp.startup.register(lambda: on_startup(app))
    dp.shutdown.register(lambda: on_shutdown(app))
    setup_application(app, dp, bot=bot)

    return app

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=CFG.port)

if __name__ == "__main__":
    main()
