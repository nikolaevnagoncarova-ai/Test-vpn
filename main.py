import os
import asyncio
import logging
import sqlite3
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

BOT_TOKEN = os.getenv("BOT_TOKEN")
SECRET_ADMIN_CODE = os.getenv("ADMIN_SECRET", "SHVECARSKY-ADMIN-777")
DB_FILE = "vpn_shop.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

REF_BONUS = 30
TARIFS = {
    "1": {"name": "Подписка на 1 месяц", "price": 1, "months": 1},
    "3": {"name": "Подписка на 3 месяца", "price": 3, "months": 3}
}

# --- МАШИНА СОСТОЯНИЙ (FSM) ДЛЯ АДМИНКИ ---
class AdminStates(StatesGroup):
    broadcast_text = State()
    give_money_user = State()
    give_money_amount = State()
    take_money_user = State()
    take_money_amount = State()
    give_admin_user = State()
    create_key_duration = State()
    create_key_data = State()

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                referrer_id INTEGER,
                is_admin INTEGER DEFAULT 0
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('admin_code_used', '0')")
        conn.commit()

def update_user_info(user_id: int, username: str, referrer_id: int = None):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        if not cur.fetchone():
            cur.execute("INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)", (user_id, username, referrer_id))
        else:
            cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
        conn.commit()

def get_user(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT balance, referrer_id, is_admin FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def get_user_by_username(username: str):
    if not username:
        return None
    clean_username = username.replace("@", "").strip()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, balance, is_admin FROM users WHERE LOWER(username) = LOWER(?)", (clean_username,))
        return cur.fetchone()

# --- ПЕРЕХВАТЧИК (MIDDLEWARE) ДЛЯ ОБНОВЛЕНИЯ ДАННЫХ ---
class UserUpdateMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user:
            update_user_info(user.id, user.username)
        return await handler(event, data)

dp.message.middleware(UserUpdateMiddleware())
dp.callback_query.middleware(UserUpdateMiddleware())

# --- МЕНЮ КОМАНД ---
async def set_bot_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="profile", description="Профиль и баланс"),
        BotCommand(command="help", description="Инструкция по настройке"),
        BotCommand(command="admin", description="Панель администратора")
    ]
    await bot.set_my_commands(commands)

# --- КЛАВИАТУРЫ (ПОЛЬЗОВАТЕЛИ) ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="catalog")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            InlineKeyboardButton(text="📖 Инструкция", callback_data="help")
        ]
    ])

def catalog_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡️ 1 месяц — 1₽", callback_data="select_tarif_1")],
        [InlineKeyboardButton(text="🚀 3 месяца — 3₽", callback_data="select_tarif_3")],
        [InlineKeyboardButton(text="« Вернуться", callback_data="back_main")]
    ])

def confirm_kb(tarif_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"buy_confirm_{tarif_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="catalog")]
    ])

def profile_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐️ Пополнить баланс", callback_data="topup_menu")],
        [InlineKeyboardButton(text="« Вернуться", callback_data="back_main")]
    ])

def topup_amounts_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 ⭐️", callback_data="paystars_1"), InlineKeyboardButton(text="10 ⭐️", callback_data="paystars_10")],
        [InlineKeyboardButton(text="50 ⭐️", callback_data="paystars_50"), InlineKeyboardButton(text="100 ⭐️", callback_data="paystars_100")],
        [InlineKeyboardButton(text="« Назад в профиль", callback_data="profile")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="« Вернуться", callback_data="back_main")]
    ])

# --- КЛАВИАТУРЫ (АДМИН ПАНЕЛЬ) ---
def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Рассылка", callback_data="adm_broadcast"), InlineKeyboardButton(text="🔑 Создать ключи", callback_data="adm_addkey")],
        [InlineKeyboardButton(text="💰 Выдать баланс", callback_data="adm_givemoney"), InlineKeyboardButton(text="📉 Забрать баланс", callback_data="adm_takemoney")],
        [InlineKeyboardButton(text="🛡 Назначить админа", callback_data="adm_giveadmin")],
        [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="back_main")]
    ])

def admin_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить действие", callback_data="adm_cancel")]
    ])

HAPP_INSTRUCTION = (
    "📌 **Инструкция по подключению:**\n\n"
    "1. Скопируйте ваш персональный ключ.\n"
    "2. Установите приложение **Happ** (App Store / Google Play).\n"
    "3. Нажмите **«+»** в правом верхнем углу приложения.\n"
    "4. Выберите **«Импорт из буфера обмена»** (Import from Clipboard).\n"
    "5. Переключите тумблер для запуска."
)

# --- АВТОРИЗАЦИЯ АДМИНА ---
@dp.message(Command("claimadmin"))
async def claim_admin_handler(message: types.Message, command: CommandObject):
    code = command.args
    if not code:
        return await message.answer("Укажите секретный ключ доступа.")
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'admin_code_used'")
        is_used = cur.fetchone()[0]
        
        if is_used == '1':
            return await message.answer("⚠️ Одноразовый код уже был использован.")
            
        if code == SECRET_ADMIN_CODE:
            cur.execute("UPDATE config SET value = '1' WHERE key = 'admin_code_used'")
            cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (message.from_user.id,))
            conn.commit()
            await message.answer("✅ **Права администратора успешно получены!**\n\nИспользуйте команду /admin для входа в панель управления.", parse_mode="Markdown")
        else:
            await message.answer("❌ Неверный ключ доступа.")

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or user[2] == 0:
        return await message.answer("У вас нет прав доступа к этому разделу.")
        
    await state.clear()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM keys WHERE is_sold = 0")
        keys_count = cur.fetchone()[0]

    text = (
        "🛡 **Панель администратора**\n\n"
        f"👥 Всего пользователей: **{users_count}**\n"
        f"🔑 Доступных ключей: **{keys_count}**\n\n"
        "Выберите необходимое действие:"
    )
    await message.answer(text, reply_markup=admin_panel_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "adm_cancel")
async def admin_cancel_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Действие отменено.")
    await admin_panel_handler(callback.message, state)

# 1. РАССЫЛКА
@dp.callback_query(F.data == "adm_broadcast")
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Отправьте сообщение для рассылки всем пользователям бота:", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.broadcast_text)

@dp.message(AdminStates.broadcast_text)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    text_to_send = message.text or message.caption
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()

    await message.answer(f"Начинаю рассылку для {len(users)} пользователей...")
    success = 0
    for (uid,) in users:
        try:
            if message.text:
                await bot.send_message(uid, text_to_send, entities=message.entities)
            elif message.photo:
                await bot.send_photo(uid, message.photo[-1].file_id, caption=text_to_send, caption_entities=message.caption_entities)
            success += 1
            await asyncio.sleep(0.05)
        except TelegramAPIError:
            pass
            
    await message.answer(f"✅ Рассылка завершена.\nУспешно доставлено: {success} из {len(users)}")

# 2. ВЫДАТЬ БАЛАНС
@dp.callback_query(F.data == "adm_givemoney")
async def start_give_money(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите **юзернейм** пользователя (например, @username):", parse_mode="Markdown", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.give_money_user)

@dp.message(AdminStates.give_money_user)
async def process_give_money_user(message: types.Message, state: FSMContext):
    target_user = get_user_by_username(message.text)
    if not target_user:
        return await message.answer("❌ Пользователь не найден. Проверьте юзернейм или убедитесь, что он запускал бота.", reply_markup=admin_cancel_kb())
    
    await state.update_data(target_id=target_user[0], target_username=message.text)
    await message.answer("Введите сумму для начисления (в рублях):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.give_money_amount)

@dp.message(AdminStates.give_money_amount)
async def process_give_money_amount(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите корректное число.", reply_markup=admin_cancel_kb())
        
    amount = int(message.text)
    data = await state.get_data()
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, data['target_id']))
        conn.commit()
        
    await state.clear()
    await message.answer(f"✅ Баланс пользователя {data['target_username']} успешно пополнен на **{amount}₽**.", parse_mode="Markdown")
    try:
        await bot.send_message(data['target_id'], f"🎁 Администратор пополнил ваш баланс на **{amount}₽**.", parse_mode="Markdown")
    except: pass

# 3. ЗАБРАТЬ БАЛАНС
@dp.callback_query(F.data == "adm_takemoney")
async def start_take_money(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите **юзернейм** пользователя для списания средств:", parse_mode="Markdown", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.take_money_user)

@dp.message(AdminStates.take_money_user)
async def process_take_money_user(message: types.Message, state: FSMContext):
    target_user = get_user_by_username(message.text)
    if not target_user:
        return await message.answer("❌ Пользователь не найден.", reply_markup=admin_cancel_kb())
    
    await state.update_data(target_id=target_user[0], target_username=message.text, current_balance=target_user[1])
    await message.answer(f"Текущий баланс: **{target_user[1]}₽**\nВведите сумму для списания:", parse_mode="Markdown", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.take_money_amount)

@dp.message(AdminStates.take_money_amount)
async def process_take_money_amount(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("❌ Введите корректное число.", reply_markup=admin_cancel_kb())
        
    amount = int(message.text)
    data = await state.get_data()
    new_balance = max(0, data['current_balance'] - amount)
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, data['target_id']))
        conn.commit()
        
    await state.clear()
    await message.answer(f"✅ У пользователя {data['target_username']} списано **{amount}₽**. Новый баланс: {new_balance}₽.", parse_mode="Markdown")

# 4. НАЗНАЧИТЬ АДМИНА
@dp.callback_query(F.data == "adm_giveadmin")
async def start_give_admin(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите **юзернейм** пользователя для выдачи прав администратора:", parse_mode="Markdown", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.give_admin_user)

@dp.message(AdminStates.give_admin_user)
async def process_give_admin_user(message: types.Message, state: FSMContext):
    target_user = get_user_by_username(message.text)
    if not target_user:
        return await message.answer("❌ Пользователь не найден.", reply_markup=admin_cancel_kb())
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (target_user[0],))
        conn.commit()
        
    await state.clear()
    await message.answer(f"✅ Пользователь {message.text} назначен администратором.")
    try:
        await bot.send_message(target_user[0], "🛡 Вам выданы права администратора. Введите /admin для доступа.")
    except: pass

# 5. ДОБАВИТЬ КЛЮЧ
@dp.callback_query(F.data == "adm_addkey")
async def start_create_key(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите период подписки (введите цифру 1 или 3):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.create_key_duration)

@dp.message(AdminStates.create_key_duration)
async def process_create_key_duration(message: types.Message, state: FSMContext):
    if message.text not in ["1", "3"]:
        return await message.answer("❌ Введите только 1 или 3.", reply_markup=admin_cancel_kb())
    
    await state.update_data(duration=int(message.text))
    await message.answer("Введите сам конфигурационный ключ (vless://, trojan:// и т.д.):", reply_markup=admin_cancel_kb())
    await state.set_state(AdminStates.create_key_data)

@dp.message(AdminStates.create_key_data)
async def process_create_key_data(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key_text = message.text.strip()
    
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO keys (key_data, duration) VALUES (?, ?)", (key_text, data['duration']))
            conn.commit()
        await message.answer(f"✅ Ключ на {data['duration']} мес. успешно загружен в базу.")
    except sqlite3.IntegrityError:
        await message.answer("❌ Этот ключ уже существует в базе данных.")
    finally:
        await state.clear()

# --- КЛИЕНТСКАЯ ЧАСТЬ (ОСНОВНАЯ) ---
@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject):
    user_id = message.from_user.id
    username = message.from_user.username
    
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)
        if ref_id == user_id: 
            ref_id = None
            
    # Записываем реферала, если это первый вход
    update_user_info(user_id, username, ref_id)
    
    text = (
        "💎 **Добро пожаловать в shvecarskyVPN!**\n\n"
        "Высокоскоростное премиум-подключение к интернету с полной анонимностью.\n\n"
        "🌐 **Преимущества:**\n"
        "• Скорость до 1 Гбит/с\n"
        "• Обход всех блокировок\n"
        "• Мгновенная выдача ключа\n\n"
        "Выберите действие:"
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "catalog")
async def catalog_handler(callback: types.CallbackQuery):
    text = (
        "🛒 **Каталог подписок**\n\n"
        "Выберите тарифный план:\n\n"
        "• **1 месяц** — 1₽\n"
        "• **3 месяца** — 3₽\n\n"
        "Ключ выдается моментально!"
    )
    await callback.message.edit_text(text, reply_markup=catalog_kb(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("select_tarif_"))
async def select_tarif_handler(callback: types.CallbackQuery):
    tarif_id = callback.data.split("_")[2]
    tarif = TARIFS.get(tarif_id)
    if not tarif: return
        
    text = (
        f"💳 **Подтверждение покупки**\n\n"
        f"Тариф: **{tarif['name']}**\n"
        f"К оплате: **{tarif['price']}₽**\n\n"
        f"С баланса будет списано **{tarif['price']}₽**."
    )
    await callback.message.edit_text(text, reply_markup=confirm_kb(tarif_id), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy_confirm_"))
async def buy_confirm_handler(callback: types.CallbackQuery):
    tarif_id = callback.data.split("_")[2]
    tarif = TARIFS.get(tarif_id)
    user_id = callback.from_user.id
    user = get_user(user_id)
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, key_data FROM keys WHERE is_sold = 0 AND duration = ? LIMIT 1", (tarif["months"],))
        key = cur.fetchone()
        
        if not key:
            return await callback.answer("⚠️ Ключи временно закончились. Скоро пополним!", show_alert=True)
            
        if user[0] < tarif["price"]:
            return await callback.answer("❌ Недостаточно средств. Пополните баланс!", show_alert=True)
        
        cur.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (tarif["price"], user_id))
        cur.execute("UPDATE keys SET is_sold = 1 WHERE id = ?", (key[0],))
        
        if user[1]:
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (REF_BONUS, user[1]))
            try: await bot.send_message(user[1], f"🎉 Реферал купил VPN! Начислено {REF_BONUS}₽.")
            except: pass
        conn.commit()
    
    text = (
        f"✅ **Оплата прошла успешно!**\n\n"
        f"Ваш ключ ({tarif['name']}):\n"
        f"`{key[1]}`\n\n"
        f"{HAPP_INSTRUCTION}"
    )
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "topup_menu")
async def topup_menu_handler(callback: types.CallbackQuery):
    text = "⭐️ **Пополнение баланса**\n\n1 ⭐️ Telegram Stars = 1₽."
    await callback.message.edit_text(text, reply_markup=topup_amounts_kb(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("paystars_"))
async def pay_stars_handler(callback: types.CallbackQuery):
    amount = int(callback.data.split("_")[1])
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Пополнение баланса",
        description=f"Пополнение на {amount}₽",
        provider_token="", 
        currency="XTR",
        prices=[LabeledPrice(label="Звёзды", amount=amount)],
        start_parameter="topup",
        payload=f"stars_{amount}"
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    amount = message.successful_payment.total_amount
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, message.from_user.id))
        conn.commit()
    await message.answer(f"🎉 **Баланс пополнен на {amount}₽!**", reply_markup=main_menu_kb(), parse_mode="Markdown")

@dp.message(Command("profile"))
@dp.callback_query(F.data == "profile")
async def profile_handler(event: types.Message | types.CallbackQuery):
    user_id = event.from_user.id
    user = get_user(user_id)
    bot_info = await bot.me()
    
    text = (
        f"👤 **Личный кабинет**\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"💰 Баланс: **{user[0]}₽**\n\n"
        f"🤝 **Партнерская программа:**\n"
        f"Бонус за покупку друга: **{REF_BONUS}₽**\n\n"
        f"Ваша ссылка:\n`https://t.me/{bot_info.username}?start={user_id}`"
    )
    
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=profile_kb(), parse_mode="Markdown")
    else:
        await event.answer(text, reply_markup=profile_kb(), parse_mode="Markdown")

@dp.message(Command("help"))
@dp.callback_query(F.data == "help")
async def help_handler(event: types.Message | types.CallbackQuery):
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(HAPP_INSTRUCTION, reply_markup=back_kb(), parse_mode="Markdown")
    else:
        await event.answer(HAPP_INSTRUCTION, reply_markup=back_kb(), parse_mode="Markdown")

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery):
    text = (
        "💎 **Добро пожаловать в shvecarskyVPN!**\n\n"
        "Высокоскоростное премиум-подключение к интернету с полной анонимностью.\n\n"
        "🌐 **Преимущества:**\n"
        "• Скорость до 1 Гбит/с\n"
        "• Обход всех блокировок\n"
        "• Мгновенная выдача ключа\n\n"
        "Выберите действие:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="Markdown")

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
                async with session.get(render_url): pass
            except: pass
            await asyncio.sleep(600)

async def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    
    # Сбрасываем накопившиеся за время простоя апдейты, чтобы бот не спамил
    await bot.delete_webhook(drop_pending_updates=True)
    await set_bot_commands(bot)
    
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(self_ping())
    await dp.start_polling(bot, handle_as_tasks=True)

if __name__ == "__main__":
    asyncio.run(main())
