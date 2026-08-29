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

REF_BONUS = 30

# Тарифы и их стоимости
TARIFS = {
    "1": {"name": "Подписка на 1 месяц", "price": 1, "months": 1},
    "3": {"name": "Подписка на 3 месяца", "price": 3, "months": 3}
}

# --- МЕНЮ КОМАНД ---
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
                duration INTEGER DEFAULT 1,
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
        [InlineKeyboardButton(text="🛒 Купить подписку VPN", callback_data="catalog")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            InlineKeyboardButton(text="📖 Инструкция", callback_data="help")
        ]
    ])

def catalog_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ 1 месяц — 1₽", callback_data="select_tarif_1")],
        [InlineKeyboardButton(text="🚀 3 месяца — 3₽", callback_data="select_tarif_3")],
        [InlineKeyboardButton(text="« Назад в меню", callback_data="back_main")]
    ])

def confirm_kb(tarif_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и оплатить", callback_data=f"buy_confirm_{tarif_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="catalog")]
    ])

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup")],
        [InlineKeyboardButton(text="« Назад", callback_data="back_main")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Назад", callback_data="back_main")]
    ])

# --- ИНСТРУКЦИЯ HAPP ---
HAPP_INSTRUCTION = (
    "📌 **Инструкция по настройке shvecarskyVPN в Happ:**\n\n"
    "1. Нажмите на скопированный ключ выше, чтобы сохранить его.\n"
    "2. Установите и откройте приложение **Happ** (доступно в App Store / Google Play).\n"
    "3. В правом верхнем углу нажмите **«+»**.\n"
    "4. Выберите пункт **«Импорт из буфера обмена»** (Import from Clipboard).\n"
    "5. Переключите тумблер для активации защищенного соединения."
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
            try: await bot.send_message(ref_id, "🤝 По вашей реферальной ссылке зарегистрировался новый пользователь.")
            except: pass

    text = (
        "💎 **Добро пожаловать в shvecarskyVPN!**\n\n"
        "Мы предоставляем высокоскоростное премиум-подключение к интернету с полной анонимностью и без ограничений по скорости.\n\n"
        "🌐 **Преимущества сервиса:**\n"
        "• Высокая скорость до 1 Гбит/с\n"
        "• Стабильный обход блокировок\n"
        "• Поддержка всех устройств\n"
        "• Мгновенная выдача ключа после оплаты\n\n"
        "Выберите нужное действие из меню ниже:"
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "catalog")
async def catalog_handler(callback: types.CallbackQuery):
    text = (
        "🛒 **Каталог подписок shvecarskyVPN**\n\n"
        "Выберите подходящий период действия тарифного плана:\n\n"
        "• **Подписка на 1 месяц** — 1₽\n"
        "• **Подписка на 3 месяца** — 3₽\n\n"
        "Ключ выдается моментально сразу после подтверждения!"
    )
    await callback.message.edit_text(text, reply_markup=catalog_kb(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("select_tarif_"))
async def select_tarif_handler(callback: types.CallbackQuery):
    tarif_id = callback.data.split("_")[2]
    tarif = TARIFS.get(tarif_id)
    
    if not tarif:
        return await callback.answer("Тариф не найден.", show_alert=True)
        
    text = (
        f"💳 **Подтверждение покупки**\n\n"
        f"Вы выбрали: **{tarif['name']}**\n"
        f"Стоимость: **{tarif['price']}₽**\n\n"
        f"С вашего баланса будет списано **{tarif['price']}₽**.\n"
        f"Вы уверены, что хотите продолжить?"
    )
    await callback.message.edit_text(text, reply_markup=confirm_kb(tarif_id), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_confirm_"))
async def buy_confirm_handler(callback: types.CallbackQuery):
    tarif_id = callback.data.split("_")[2]
    tarif = TARIFS.get(tarif_id)
    user_id = callback.from_user.id
    user = get_user(user_id)
    
    if not tarif:
        return await callback.answer("Ошибка тарифа.", show_alert=True)

    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        
        # Ищем ключ под выбранный период
        cur.execute("SELECT id, key_data FROM keys WHERE is_sold = 0 AND duration = ? LIMIT 1", (tarif["months"],))
        key = cur.fetchone()
        
        if not key:
            await callback.answer(f"⚠️ Ключи на {tarif['name']} временно закончились. Скоро пополним!", show_alert=True)
            return
            
        if user[0] < tarif["price"]:
            await callback.answer(f"❌ Недостаточно средств на балансе. Требуется: {tarif['price']}₽", show_alert=True)
            return
        
        # Списание и обновление статуса
        cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (tarif["price"], user_id))
        cur.execute("UPDATE keys SET is_sold = 1 WHERE id = ?", (key[0],))
        
        # Бонус рефералу
        if user[1]:
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (REF_BONUS, user[1]))
            try: await bot.send_message(user[1], f"🎉 Ваш реферал купил VPN! Вам начислено {REF_BONUS}₽.")
            except: pass
            
        conn.commit()
    
    text = (
        f"✅ **Оплата прошла успешно!**\n\n"
        f"Ваш персональный ключ ({tarif['name']}):\n\n"
        f"`{key[1]}`\n\n"
        f"*(Нажмите на ключ, чтобы скопировать в буфер обмена)*\n\n"
        f"{HAPP_INSTRUCTION}"
    )
    
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
    await callback.answer()

@dp.message(Command("profile"))
@dp.callback_query(F.data == "profile")
async def profile_handler(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    user = get_user(user_id)
    bot_info = await bot.me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    text = (
        f"👤 **Личный кабинет**\n\n"
        f"🆔 Ваш Telegram ID: `{user_id}`\n"
        f"💰 Ваш баланс: **{user[0]}₽**\n\n"
        f"🤝 **Партнерская система:**\n"
        f"Приглашайте друзей и получайте бонусом **{REF_BONUS}₽** на свой баланс за каждую их покупку!\n\n"
        f"Ваша реферальная ссылка:\n`{ref_link}`"
    )
    
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=profile_kb(), parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, reply_markup=profile_kb(), parse_mode="Markdown")

@dp.message(Command("help"))
@dp.callback_query(F.data == "help")
async def help_handler(event: types.Message | types.CallbackQuery):
    text = f"{HAPP_INSTRUCTION}"
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, reply_markup=back_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    text = (
        "💎 **Добро пожаловать в shvecarskyVPN!**\n\n"
        "Мы предоставляем высокоскоростное премиум-подключение к интернету с полной анонимностью и без ограничений по скорости.\n\n"
        "🌐 **Преимущества сервиса:**\n"
        "• Высокая скорость до 1 Гбит/с\n"
        "• Стабильный обход блокировок\n"
        "• Поддержка всех устройств\n"
        "• Мгновенная выдача ключа после оплаты\n\n"
        "Выберите нужное действие из меню ниже:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "topup")
async def topup_dummy(callback: types.CallbackQuery):
    await callback.answer("💳 Модуль автоматической оплаты подключается. Используйте администратора для пополнения.", show_alert=True)

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("addkey"), F.from_user.id == ADMIN_ID)
async def admin_add_key(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer(
            "⚠️ **Формат команды:**\n`/addkey [месяцы] [ключ]`\n\n"
            "Пример на 1 месяц:\n`/addkey 1 vless://ключ123`\n"
            "Пример на 3 месяца:\n`/addkey 3 vless://ключ123`", 
            parse_mode="Markdown"
        )
    
    try:
        args = command.args.split(maxsplit=1)
        duration = int(args[0])
        key_data = args[1].strip()
        
        if duration not in [1, 3]:
            return await message.answer("❌ Укажите длительность 1 или 3 месяца.")

        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO keys (key_data, duration) VALUES (?, ?)", (key_data, duration))
            conn.commit()
        await message.answer(f"✅ Ключ на **{duration} мес.** успешно добавлен!", parse_mode="Markdown")
    except sqlite3.IntegrityError:
        await message.answer("❌ Этот ключ уже есть в базе.")
    except Exception:
        await message.answer("❌ Ошибка формата. Пример: `/addkey 1 vless://ссылка`", parse_mode="Markdown")

@dp.message(Command("givemoney"), F.from_user.id == ADMIN_ID)
async def admin_give_money(message: types.Message, command: CommandObject):
    if not command.args:
        return await message.answer("⚠️ Формат: `/givemoney [ID] [Сумма]`", parse_mode="Markdown")
    
    try:
        args = command.args.split()
        target_id = int(args[0])
        amount = int(args[1])
        
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target_id))
            if cur.rowcount == 0:
                return await message.answer("❌ Пользователь с таким ID не найден.")
            conn.commit()
            
        await message.answer(f"✅ Успешно выдано **{amount}₽** пользователю `{target_id}`.", parse_mode="Markdown")
        try:
            await bot.send_message(target_id, f"💰 Ваш баланс пополнен на **{amount}₽**!", parse_mode="Markdown")
        except: pass
            
    except (ValueError, IndexError):
        await message.answer("❌ Ошибка формата. Пример: `/givemoney 123456789 10`", parse_mode="Markdown")

# --- WEB СЕРВЕР RENDER ---
async def handle_ping(request):
    return web.Response(text="shvecarskyVPN is active.")

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
