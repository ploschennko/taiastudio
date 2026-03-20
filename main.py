import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart
from aiocryptopay import AioCryptoPay, Networks
from aiocryptopay.models.update import Update

# Вставте ваші токени (отримані в @BotFather та @CryptoBot)
BOT_TOKEN = "ВАШ_ТОКЕН_BOTFATHER"
CRYPTO_TOKEN = "ВАШ_ТОКЕН_CRYPTOBOT"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Ініціалізація CryptoBot API. Для тестів можна змінити на Networks.TEST_NET
crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
web_app = web.Application()

# --- КРОК 1. Вхід ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=)
    await message.answer("Це система, не курс. ⚙️", reply_markup=kb)

# --- КРОК 2. Міні-прогрів ---
@dp.callback_query(F.data == "warmup")
async def process_warmup(callback: CallbackQuery):
    await callback.message.answer("Що варто знати: це не про промпти.\nЦе про архітектуру та автоматизацію.")
    
    # Імітація затримки перед наступним повідомленням
    await asyncio.sleep(2)
    await callback.message.answer("Люди не заробляють через відсутність структури.\nХаос не масштабується.")
    
    await asyncio.sleep(2)
    kb = InlineKeyboardMarkup(inline_keyboard=,
       )
    await callback.message.answer("Обери свій формат взаємодії 👇", reply_markup=kb)

# --- КРОК 3. Гілка "Навчитися" (Тарифи BASE та PRO) ---
@dp.callback_query(F.data == "diy")
async def process_diy(callback: CallbackQuery):
    # Генеруємо унікальний рахунок для BASE. Payload зберігає ID клієнта для автоматизації
    inv_base = await crypto.create_invoice(asset='USDT', amount=39, description="Формат BASE", payload=str(callback.from_user.id))
    kb_base = InlineKeyboardMarkup(inline_keyboard=)
    await callback.message.answer("🥉 *Формат BASE*\nДоступ до системи та матеріалів.\n❌ Без підтримки. Сам відповідаєш за результат.", reply_markup=kb_base, parse_mode="Markdown")
    
    await asyncio.sleep(1)
    
    # Генеруємо унікальний рахунок для PRO
    inv_pro = await crypto.create_invoice(asset='USDT', amount=79, description="Формат PRO 🔥", payload=str(callback.from_user.id))
    kb_pro = InlineKeyboardMarkup(inline_keyboard=)
    await callback.message.answer("🥈 *Формат PRO 🔥*\nВсе з BASE + закритий чат та мій особистий фідбек.\n✅ Ти робиш, я направляю.", reply_markup=kb_pro, parse_mode="Markdown")

# --- КРОК 4. Гілка "Замовити під себе" (Тариф MENTORSHIP) ---
@dp.callback_query(F.data == "dfy")
async def process_dfy(callback: CallbackQuery):
    inv_mentor = await crypto.create_invoice(asset='USDT', amount=200, description="Формат MENTORSHIP", payload=str(callback.from_user.id))
    kb_mentor = InlineKeyboardMarkup(inline_keyboard=,
       )
    await callback.message.answer("🥇 *Формат MENTORSHIP*\nМи доводимо тебе до результату під ключ.\nБоти, AI-системи, сайти.", reply_markup=kb_mentor, parse_mode="Markdown")

# --- КРОК 5. Автоматична видача доступу після оплати (Webhook від CryptoBot) ---
@crypto.pay_handler()
async def invoice_paid(update: Update, app) -> None:
    # Система витягує ID користувача, який ми передали в payload при генерації рахунку
    user_id = int(update.payload) 
    
    # Відправляємо клієнту доступ
    await bot.send_message(
        chat_id=user_id, 
        text="Оплата успішна ⚡️\nОсь твоя інструкція та доступ до системи: [ПОСИЛАННЯ НА ПЛАТФОРМУ/ПАПКУ]\n\n🔑 Твоє персональне запрошення у закритий чат: [ЛІНК]"
    )

async def main():
    logging.basicConfig(level=logging.INFO)
    
    # Налаштування сервера для прослуховування вебхуків від CryptoBot
    crypto.get_updates(web_app, path='/cryptobot_webhook')
    runner = web.AppRunner(web_app)
    await runner.setup()
    # Сервер слухатиме порт 3001 (можна змінити під ваші налаштування хостингу)
    site = web.TCPSite(runner, '0.0.0.0', 3001)
    await site.start()
    
    # Запуск Telegram-бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
