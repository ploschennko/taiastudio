import asyncio
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from aiohttp import web

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
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
# Конфигурация и Архитектура
# =========================

class PayProvider(str, Enum):
    STARS = "STARS"          # Основной метод для цифровых товаров (требование Telegram)
    CRYPTOPAY = "CRYPTOPAY"  # Альтернативный метод для криптовалюты
    LINK = "LINK"            # Внешние ссылки на оплату


@dataclass(frozen=True)
class Config:
    bot_token: str
    tg_webhook_path: str
    tg_webhook_secret: str
    public_base_url: str
    port: int
    pay_provider: PayProvider
    base_stars: int
    pro_stars: int
    mentor_stars: int
    base_pay_url: Optional[str]
    pro_pay_url: Optional[str]
    mentor_pay_url: Optional[str]
    manager_url: str
    paysupport_text: str
    crypto_token: Optional[str]
    crypto_webhook_path: str


def must_env(name: str) -> str:
    """Получает обязательные переменные окружения."""
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Критическая ошибка: отсутствует переменная окружения {name}")
    return v


def get_public_base_url() -> str:
    """Определяет публичный URL (сначала ищет ручную настройку, затем авто-настройки хостинга Render)."""
    manual = os.getenv("PUBLIC_BASE_URL", "").strip()
    if manual:
        return manual.rstrip("/")
    auto = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if auto:
        return auto.rstrip("/")
    raise RuntimeError("PUBLIC_BASE_URL или RENDER_EXTERNAL_URL должны быть заданы для вебхуков.")


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

# Валидация путей
if not CFG.tg_webhook_path.startswith("/"):
    raise RuntimeError("TG_WEBHOOK_PATH должен начинаться со слэша (/)")
if not CFG.crypto_webhook_path.startswith("/"):
    raise RuntimeError("CRYPTO_WEBHOOK_PATH должен начинаться со слэша (/)")


# =========================
# Инициализация инфраструктуры
# =========================

bot = Bot(token=CFG.bot_token, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Глобальный клиент CryptoPay (инициализируется один раз во избежание утечек сессий)
crypto_client: Optional[AioCryptoPay] = None
if CFG.crypto_token:
    crypto_client = AioCryptoPay(token=CFG.crypto_token, network=Networks.MAIN_NET)


# =========================
# Тексты воронки
# =========================

TEXT_START = "<b>Це система, не курс. ⚙️</b>"

TEXT_STEP3 = "Що варто знати: це не про промпти. Це про <b>архітектуру</b> та <b>автоматизацію</b>."
TEXT_STEP4 = "Люди не заробляють через відсутність структури. <b>Хаос не масштабується.</b>"
TEXT_STEP5 = "<b>Обери свій формат взаємодії 👇</b>"

TEXT_BASE = "🥉 <b>Формат BASE.</b> Доступ до системи та матеріалів. <i>Без підтримки.</i> Сам відповідаєш за результат."
TEXT_PRO = "🥈 <b>Формат PRO 🔥.</b> Все з BASE + закритий чат та мій особистий фідбек. Ти робиш, я направляю."
TEXT_MENTOR = "🥇 <b>Формат MENTORSHIP.</b> Ми доводимо тебе до результату під ключ. Боти, AI-системи, сайти."


# =========================
# Callback Data
# =========================

CB_CONTINUE = "f:continue"
CB_LEARN = "f:learn"
CB_ORDER = "f:order"
CB_PAY_BASE = "pay:base"
CB_PAY_PRO = "pay:pro"
CB_PAY_MENTOR = "pay:mentor"


# =========================
# Клавиатуры
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
    """Динамическое создание кнопки оплаты в зависимости от выбранного провайдера."""
    if CFG.pay_provider == PayProvider.LINK and url:
        return InlineKeyboardButton(text=label, url=url)
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
# Утилиты и механики задержек
# =========================

async def typing_pause(chat_id: int, seconds: float = 2.0) -> None:
    """Имитирует печать сообщения для удержания внимания."""
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        await asyncio.sleep(seconds)
    except Exception as e:
        logging.warning(f"Ошибка паузы (typing) для чата {chat_id}: {e}")


# =========================
# Обработчики воронки (Роутинг)
# =========================

@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(TEXT_START, reply_markup=kb_continue())

@router.callback_query(F.data == CB_CONTINUE)
async def step_continue(cb: CallbackQuery) -> None:
    await cb.answer()
    
    # Убираем кнопку, чтобы не нажимали дважды
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
    await asyncio.sleep(0.6) # Небольшая пауза для читабельности
    await cb.message.answer(TEXT_PRO, reply_markup=kb_pay_pro())

@router.callback_query(F.data == CB_ORDER)
async def branch_order(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer(TEXT_MENTOR, reply_markup=kb_pay_mentor())

@router.message(Command("paysupport"))
async def paysupport(message: Message) -> None:
    await message.answer(CFG.paysupport_text, reply_markup=kb_manager())


# =========================
# Оплата: Telegram Stars и CryptoPay
# =========================

async def send_stars_invoice(message: Message, title: str, description: str, amount_stars: int, payload: str) -> None:
    """Создает и отправляет инвойс в Telegram Stars (XTR)."""
    try:
        await message.answer_invoice(
            title=title,
            description=description,
            prices=[LabeledPrice(label=title, amount=amount_stars)],
            payload=payload, 
            currency="XTR",
            provider_token="", # Обязательно пустое поле для Stars
        )
    except Exception as e:
        logging.error(f"Ошибка создания инвойса Stars: {e}")
        await message.answer("Сталася помилка при створенні інвойсу. Зверніться до підтримки.", reply_markup=kb_manager())

@router.callback_query(F.data.in_({CB_PAY_BASE, CB_PAY_PRO, CB_PAY_MENTOR}))
async def on_pay_clicked(cb: CallbackQuery) -> None:
    await cb.answer()
    msg = cb.message
    user_id = cb.from_user.id

    if CFG.pay_provider == PayProvider.LINK:
        await msg.answer("Оплата за посиланням зараз не налаштована. Напишіть менеджеру.", reply_markup=kb_manager())
        return

    if CFG.pay_provider == PayProvider.STARS:
        if cb.data == CB_PAY_BASE:
            await send_stars_invoice(msg, "BASE", "Доступ до системи та матеріалів. Без підтримки.", CFG.base_stars, f"base:{user_id}")
        elif cb.data == CB_PAY_PRO:
            await send_stars_invoice(msg, "PRO", "Все з BASE + чат та фідбек.", CFG.pro_stars, f"pro:{user_id}")
        else:
            await send_stars_invoice(msg, "MENTORSHIP", "Під ключ: боти, AI‑системи, сайти.", CFG.mentor_stars, f"mentor:{user_id}")
        return

    # Если выбран CRYPTOPAY
    if CFG.pay_provider == PayProvider.CRYPTOPAY:
        if not crypto_client:
            await msg.answer("Crypto Pay не налаштований. Напишіть менеджеру.", reply_markup=kb_manager())
            return

        try:
            if cb.data == CB_PAY_BASE:
                inv = await crypto_client.create_invoice(asset="USDT", amount=39, description="BASE", payload=f"{user_id}:BASE")
            elif cb.data == CB_PAY_PRO:
                inv = await crypto_client.create_invoice(asset="USDT", amount=79, description="PRO", payload=f"{user_id}:PRO")
            else:
                inv = await crypto_client.create_invoice(asset="USDT", amount=200, description="MENTORSHIP", payload=f"{user_id}:MENTOR")
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатити в CryptoBot", url=inv.bot_invoice_url)]])
            await msg.answer("Інвойс створено. Оплатіть та дочекайтеся підтвердження ✅", reply_markup=kb)
        except Exception as e:
            logging.error(f"Ошибка генерации крипто-инвойса: {e}")
            await msg.answer("Помилка генерації крипто-інвойсу. Напишіть менеджеру.", reply_markup=kb_manager())

@router.pre_checkout_query()
async def pre_checkout(pre: PreCheckoutQuery) -> None:
    """Подтверждение готовности провести транзакцию (обязательное требование Telegram)."""
    try:
        await pre.answer(ok=True)
    except Exception as e:
        logging.error(f"Ошибка подтверждения PreCheckoutQuery ID {pre.id}: {e}")

@router.message(F.successful_payment)
async def on_success_payment(message: Message) -> None:
    """Обработка успешной оплаты через Telegram Stars."""
    p = message.successful_payment.invoice_payload
    product = p.split(":")[0]
    
    if product == "base":
        await message.answer("Оплата успішна ✅\nДоступ буде виданий менеджером.", reply_markup=kb_manager())
    elif product == "pro":
        await message.answer("Оплата успішна ✅\nДалі: менеджер додасть у чат та надасть доступ.", reply_markup=kb_manager())
    else:
        await message.answer("Оплата успішна ✅\nДалі: менеджер стартує MENTORSHIP.", reply_markup=kb_manager())


# =========================
# Безопасность и Webhook Crypto Pay
# =========================

def verify_cryptopay_signature(raw_body: bytes, signature_header: str, token: str) -> bool:
    """Математическая проверка подписи HMAC-SHA256 для защиты вебхуков CryptoPay."""
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    calc = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    # Использование compare_digest предотвращает атаки по времени (timing attacks)
    return hmac.compare_digest(calc, signature_header)

async def cryptopay_webhook(request: web.Request) -> web.Response:
    """Обрабатывает входящие данные от Crypto Pay."""
    if CFG.pay_provider != PayProvider.CRYPTOPAY:
        return web.Response(status=404, text="Сервис CryptoPay отключен.")

    if not CFG.crypto_token:
        return web.Response(status=500, text="Отсутствует токен CryptoPay.")

    raw = await request.read()
    sig = request.headers.get("crypto-pay-api-signature", "")
    
    if not sig or not verify_cryptopay_signature(raw, sig, CFG.crypto_token):
        logging.warning("Обнаружена неверная подпись вебхука CryptoPay.")
        return web.Response(status=401, text="Ошибка проверки подписи.")

    try:
        data = json.loads(raw.decode("utf-8"))
        # Используем Pydantic V2 model_validate
        upd = CryptoUpdate.model_validate(data)
    except Exception as e:
        logging.exception(f"Ошибка валидации данных CryptoPay: {e}")
        return web.Response(status=400, text="Неверный формат JSON.")

    if upd.update_type == "invoice_paid":
        inv = upd.payload
        # Безопасное извлечение payload
        custom_payload = getattr(inv, "payload", "") or ""
        invoice_id = getattr(inv, "invoice_id", None)
        
        try:
            user_str, plan = custom_payload.split(":")
            user_id = int(user_str)
        except ValueError:
            logging.warning(f"Ошибка парсинга payload: {custom_payload}")
            return web.Response(text="Payload обработан с ошибкой.")

        if invoice_id is not None:
            logging.info(f"Успешная транзакция -> ID: {invoice_id} | Plan: {plan} | User: {user_id}")

        try:
            if plan == "BASE":
                await bot.send_message(user_id, "Оплата успішна ⚡️\nДоступ буде виданий менеджером.", reply_markup=kb_manager())
            elif plan == "PRO":
                await bot.send_message(user_id, "Оплата успішна 🔥\nДалі: менеджер додасть у чат та надасть доступ.", reply_markup=kb_manager())
            else:
                await bot.send_message(user_id, "Оплата успішна 🚀\nДалі: менеджер стартує MENTORSHIP.", reply_markup=kb_manager())
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения пользователю {user_id}: {e}")

    return web.Response(text="Событие успешно обработано.")


# =========================
# Запуск и остановка приложения
# =========================

async def on_startup(app: web.Application) -> None:
    """Устанавливает вебхук Telegram при запуске сервера."""
    webhook_url = f"{CFG.public_base_url}{CFG.tg_webhook_path}"
    await bot.set_webhook(
        url=webhook_url,
        secret_token=CFG.tg_webhook_secret,
        drop_pending_updates=True,
    )
    logging.info(f"Telegram Webhook установлен: {webhook_url}")

async def on_shutdown(app: web.Application) -> None:
    """Закрывает сессии сети для предотвращения утечек памяти."""
    await bot.session.close()
    if crypto_client:
        await crypto_client.close()
    logging.info("Сетевые сессии успешно закрыты.")

def create_app() -> web.Application:
    """Создает aiohttp приложение с роутами вебхуков."""
    app = web.Application()

    async def health(_req: web.Request) -> web.Response:
        return web.Response(text="Система работает штатно.")

    app.router.add_get("/health", health)

    request_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=CFG.tg_webhook_secret,
        handle_in_background=True, 
    )
    request_handler.register(app, path=CFG.tg_webhook_path)

    app.router.add_post(CFG.crypto_webhook_path, cryptopay_webhook)

    dp.startup.register(lambda: on_startup(app))
    dp.shutdown.register(lambda: on_shutdown(app))
    setup_application(app, dp, bot=bot)

    return app

def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=CFG.port)

if __name__ == "__main__":
    main()
