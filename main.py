import os
import asyncio
import logging
import sqlite3
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
DB_FILE = "vpn_shop.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

PRICE_PER_KEY = 150
REF_BONUS = 30

# --- МЕНЮ КОМАНД (ПОДСКАЗКИ) ---
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="profile", description="Профиль и баланс"),
        BotCommand(command="help", description="Инструкция по настройке")
    ]
    await bot.set_my_commands(commands)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                referrer_id INTEGER
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_data TEXT UNIQUE,
                is_sold INTEGER DEFAULT 0
            )
        """)
        conn.commit()

def get_user(user_id):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance, referrer_id FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def add_user(user_id, referrer_id=None):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO users (user_id, referrer_id) VALUES (?, ?)", (user_id, referrer_id))
        conn.commit()

# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить ключ", callback_data="buy_key")],
        [
            InlineKeyboardButton(text="Профиль", callback_data="profile"),
            InlineKeyboardButton(text="Инструкция", callback_data="help")
        ]
    ])

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пополнить баланс", callback_data="topup")],
        [InlineKeyboardButton(text="Назад", callback_data="back_main")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад", callback_data="back_main")]
    ])

# --- ТЕКСТ ИНСТРУКЦИИ ДЛЯ HAPP ---
HAPP_INSTRUCTION = (
    "📌 **Инструкция по подключению в Happ:**\n\n"
    "1. Скачайте приложение **Happ** из App Store или Google Play.\n"
    "2. Нажмите на скопированный ключ выше, чтобы скопировать его.\n"
    "3. Откройте приложение Happ.\n"
    "4. Нажмите значок **+** в верхнем углу и выберите **«Импорт из буфера обмена»** (Import from Clipboard).\n"
    "5. Нажмите тумблер включения для подключения к VPN."
)

# --- КЛИЕНТСКАЯ ЧАСТЬ ---
@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject):
    user_id = message.from_user.id
    if not get_user(user_id):
        ref_id = None
        if command.args and command.args.isdigit():
            ref_id = int(command.args)
            if ref_id == user_id: 
                ref_id = None
        add_user(user_id, ref_id)
        if ref_id:
            try: await bot.send_message(ref_id, "По вашей реферальной ссылке зарегистрировался новый пользователь.")
            except: pass

    text = (
        "Добро пожаловать в **shvecarskyVPN**.\n\n"
        "Высокоскоростное и стабильное подключение без ограничений. "
        "Оптимизировано для работы с приложением Happ."
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@dp.message(Command("profile"))
@dp.callback_query(F.data == "profile")
async def profile_handler(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    user = get_user(user_id)
    bot_info = await bot.me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    text = (
        f"**Личный кабинет**\n\n"
        f"ID: `{user_id}`\n"
        f"Баланс: **{user[0]} руб.**\n\n"
        f"**Реферальная программа:**\n"
        f"Ваша ссылка:\n`{ref_link}`\n\n"
        f"Получайте {REF_BONUS} руб. на баланс с каждой покупки приглашенного пользователя."
    )
    
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=profile_kb(), parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, reply_markup=profile_kb(), parse_mode="Markdown")

@dp.message(Command("help"))
@dp.callback_query(F.data == "help")
async def help_handler(event: types.Message | types.CallbackQuery):
    text = (
        f"**Настройка shvecarskyVPN**\n\n"
        f"{HAPP_INSTRUCTION}"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, reply_markup=back_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "buy_key")
async def buy_key_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id)
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, key_data FROM keys WHERE is_sold = 0 LIMIT 1")
        key = cur.fetchone()
        
        if not key:
            await callback.answer("Ключи временно отсутствуют. Скоро пополним!", show_alert=True)
            return
            
        if user[0] < PRICE_PER_KEY:
            await callback.answer(f"Недостаточно средств. Стоимость ключа: {PRICE_PER_KEY} руб.", show_alert=True)
            return
        
        # Списание средств и выдача ключа
        cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (PRICE_PER_KEY, user_id))
        cur.execute("UPDATE keys SET is_sold = 1 WHERE id = ?", (key[0],))
        
        # Начисление бонусу пригласившему
        if user[1]:
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (REF_BONUS, user[1]))
            try: await bot.send_message(user[1], f"Ваш реферал совершил покупку. Начислено {REF_BONUS} руб.")
            except: pass
            
        conn.commit()
    
    text = (
        f"🔑 **Ваш ключ shvecarskyVPN:**\n\n"
        f"`{key[1]}`\n\n"
        f"*(Нажмите на код выше, чтобы скопировать)*\n\n"
        f"{HAPP_INSTRUCTION}"
    )
    
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    text = (
        "Добро пожаловать в **shvecarskyVPN**.\n\n"
        "Высокоскоростное и стабильное подключение без ограничений. "
        "Оптимизировано для работы с приложением Happ."
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "topup")
async def topup_dummy(callback: types.CallbackQuery):
    await callback.answer("Раздел пополнения в процессе подключения.", show_alert=True)

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("addkey"), F.from_user.id == ADMIN_ID)
async def admin_add_key(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Формат: `/addkey vless://ключ`", parse_mode="Markdown")
    
    key_data = command.args.strip()
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO keys (key_data) VALUES (?)", (key_data,))
            conn.commit()
        await message.answer("Ключ успешно добавлен.")
    except sqlite3.IntegrityError:
        await message.answer("Ошибка: Такой ключ уже существует в базе.")

@dp.message(Command("givemoney"), F.from_user.id == ADMIN_ID)
async def admin_give_money(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("Формат: `/givemoney [ID] [Сумма]`", parse_mode="Markdown")
    
    try:
        args = command.args.split()
        target_id = int(args[0])
        amount = int(args[1])
        
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target_id))
            if cur.rowcount == 0:
                return await message.answer("Пользователь с таким ID не найден.")
            conn.commit()
            
        await message.answer(f"Выдано {amount} руб. пользователю `{target_id}`.", parse_mode="Markdown")
        try:
            await bot.send_message(target_id, f"Ваш баланс пополнен на **{amount} руб.**", parse_mode="Markdown")
        except:
            pass
            
    except (ValueError, IndexError):
        await message.answer("Ошибка формата. Пример: `/givemoney 123456789 500`", parse_mode="Markdown")

# --- WEB СЕРВЕР ДЛЯ RENDER ---
async def handle_ping(request):
    return web.Response(text="shvecarskyVPN Bot is running.")

async def self_ping():
    await asyncio.sleep(10)
    port = os.getenv("PORT", "8080")
    render_url = os.getenv("RENDER_EXTERNAL_URL", f"http://127.0.0.1:{port}")
    async with ClientSession() as session:
        while True:
            try:
                async with session.get(render_url) as resp: pass
            except: pass
            await asyncio.sleep(600)

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    await set_bot_commands(bot)
    
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(self_ping())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
