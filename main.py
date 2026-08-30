import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramAPIError

BOT_TOKEN = os.getenv("BOT_TOKEN")
SECRET_ADMIN_CODE = os.getenv("ADMIN_SECRET", "GOROSHEK-ADMIN-777")

# Данные для Platega (лучше вынести в Environment Variables на Render)
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID", "YOUR_MERCHANT_ID")
PLATEGA_SECRET_KEY = os.getenv("PLATEGA_SECRET_KEY", "YOUR_SECRET_KEY")
PLATEGA_API_URL = os.getenv("PLATEGA_API_URL", "https://app.platega.io")

DB_FILE = "goroshek_vpn.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

REF_PERCENT = 0.10  # 10% от суммы покупки друга
TARIFS = {
    "1": {"name": "Подписка на 1 месяц", "price": 119.0, "months": 1},
    "3": {"name": "Подписка на 3 месяца", "price": 309.0, "months": 3},
    "6": {"name": "Подписка на 6 месяцев", "price": 589.0, "months": 6},
    "12": {"name": "Подписка на 12 месяцев", "price": 979.0, "months": 12}
}

# --- МАШИНА СОСТОЯНИЙ (FSM) ---
class AdminStates(StatesGroup):
    broadcast_text = State()
    give_money_user = State()
    give_money_amount = State()
    take_money_user = State()
    take_money_amount = State()
    give_admin_user = State()
    create_key_duration = State()
    create_key_data = State()

class TopUpStates(StatesGroup):
    custom_amount = State()

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0.0,
                referrer_id INTEGER,
                is_admin INTEGER DEFAULT 0,
                sub_expires TEXT DEFAULT NULL,
                key_data TEXT DEFAULT NULL
            )
        """)
        cursor.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in cursor.fetchall()]
        if "sub_expires" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN sub_expires TEXT DEFAULT NULL")
        if "key_data" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN key_data TEXT DEFAULT NULL")

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
        cur.execute("SELECT balance, referrer_id, is_admin, sub_expires, key_data FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def get_referrals_count(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users WHERE referrer_id = ?", (user_id,))
        return cur.fetchone()[0]

def get_user_by_username(username: str):
    if not username:
        return None
    clean_username = username.replace("@", "").strip()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, balance, is_admin FROM users WHERE LOWER(username) = LOWER(?)", (clean_username,))
        return cur.fetchone()

# --- ПЕРЕХВАТЧИК (MIDDLEWARE) ---
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
        BotCommand(command="profile", description="Личный кабинет"),
        BotCommand(command="admin", description="Админ-панель")
    ]
    await bot.set_my_commands(commands)

# --- КЛАВИАТУРЫ ---
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Купить подписку", callback_data="catalog")],
        [
            InlineKeyboardButton(text="👤 Профиль", callback_data="profile"),
            InlineKeyboardButton(text="👥 Рефералы", callback_data="referral_menu")
        ],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="help")]
    ])

def catalog_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌱 1 месяц — 119 ₽", callback_data="select_tarif_1")],
        [InlineKeyboardButton(text="🌿 3 месяца — 309 ₽", callback_data="select_tarif_3")],
        [InlineKeyboardButton(text="🍀 6 месяцев — 589 ₽ | Выгодно 🔥", callback_data="select_tarif_6")],
        [InlineKeyboardButton(text="🌳 12 месяцев — 979 ₽ | Супер-выгодно 🔥", callback_data="select_tarif_12")],
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")]
    ])

def confirm_kb(tarif_id: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить покупку", callback_data=f"buy_confirm_{tarif_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="catalog")]
    ])

def profile_kb(has_sub: bool):
    kb = []
    if has_sub:
        kb.append([InlineKeyboardButton(text="🔑 Мой ключ доступа", callback_data="show_my_key")])
    kb.append([InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup_menu")])
    kb.append([InlineKeyboardButton(text="📄 Польз.Соглашение", url="https://telegra.ph/Polzovatelskoe-soglashenie-08-01-39")])
    kb.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def referral_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")]
    ])

def topup_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить через Platega (Рубли)", callback_data="topup_platega_menu")],
        [InlineKeyboardButton(text="⭐️ Пополнить через Telegram Stars", callback_data="topup_stars_menu")],
        [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="profile")]
    ])

def topup_platega_amounts_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="119 ₽", callback_data="platega_pay_119"), InlineKeyboardButton(text="309 ₽", callback_data="platega_pay_309")],
        [InlineKeyboardButton(text="589 ₽", callback_data="platega_pay_589"), InlineKeyboardButton(text="979 ₽", callback_data="platega_pay_979")],
        [InlineKeyboardButton(text="✍️ Ввести свою сумму", callback_data="platega_custom_amount")],
        [InlineKeyboardButton(text="◀️ Назад к выбору метода", callback_data="topup_menu")]
    ])

def topup_stars_amounts_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="119 ⭐️", callback_data="paystars_119"), InlineKeyboardButton(text="309 ⭐️", callback_data="paystars_309")],
        [InlineKeyboardButton(text="589 ⭐️", callback_data="paystars_589"), InlineKeyboardButton(text="979 ⭐️", callback_data="paystars_979")],
        [InlineKeyboardButton(text="◀️ Назад к выбору метода", callback_data="topup_menu")]
    ])

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")]
    ])

def cancel_topup_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_platega_menu")]
    ])

# --- АДМИН ПАНЕЛЬ КЛАВИАТУРЫ ---
def admin_panel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Рассылка", callback_data="adm_broadcast"), InlineKeyboardButton(text="🔑 Добавить ключ", callback_data="adm_addkey")],
        [InlineKeyboardButton(text="💰 Выдать баланс", callback_data="adm_givemoney"), InlineKeyboardButton(text="📉 Забрать баланс", callback_data="adm_takemoney")],
        [InlineKeyboardButton(text="🛡 Дать админа", callback_data="adm_giveadmin")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="back_main")]
    ])

def admin_cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="adm_cancel")]
    ])

HAPP_INSTRUCTION = (
    "🌿 <b>Инструкция по подключению Горошек VPN:</b>\n\n"
    "1. Скопируйте ваш уникальный ключ доступа из сообщения выше.\n"
    "2. Скачайте официальное приложение <b>Happ</b> (доступно в App Store и Google Play).\n"
    "3. Откройте приложение и нажмите на значок <b>«+»</b> в правом верхнем углу.\n"
    "4. Выберите пункт <b>«Импорт из буфера обмена»</b>.\n"
    "5. Включите защищенное соединение и наслаждайтесь свободным интернетом."
)

# --- ФУНКЦИЯ СОЗДАНИЯ ПЛАТЕЖА PLATEGA ---
async def create_platega_payment(amount: float, user_id: int, username: str):
    order_id = f"topup_{user_id}_{int(datetime.now().timestamp())}"
    headers = {
        "X-MerchantId": PLATEGA_MERCHANT_ID,
        "X-Secret": PLATEGA_SECRET_KEY,
        "Content-Type": "application/json"
    }
    
    payload = {
        "paymentDetails": {
            "amount": amount,
            "currency": "RUB"
        },
        "orderId": order_id,
        "description": f"Пополнение баланса Горошек VPN на {amount} ₽",
        "returnUrl": "https://t.me/" + (username if username else "bot"),
        "failUrl": "https://t.me/" + (username if username else "bot"),
        "metadata": {
            "telegram_id": user_id,
            "amount": amount
        }
    }

    async with ClientSession() as session:
        try:
            async with session.post(f"{PLATEGA_API_URL}/transaction/create", json=payload, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get("paymentUrl") or data.get("url")
                else:
                    logging.error(f"Platega error: {await response.text()}")
                    return None
        except Exception as e:
            logging.error(f"Platega connection error: {e}")
            return None

# --- АВТОРИЗАЦИЯ АДМИНА ---
@dp.message(Command("claimadmin"))
async def claim_admin_handler(message: types.Message, command: CommandObject):
    code = command.args
    if not code:
        return await message.answer("Введите секретный код.")
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'admin_code_used'")
        is_used = cur.fetchone()[0]
        
        if is_used == '1':
            return await message.answer("Код уже был активирован ранее.")
            
        if code == SECRET_ADMIN_CODE:
            cur.execute("UPDATE config SET value = '1' WHERE key = 'admin_code_used'")
            cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (message.from_user.id,))
            conn.commit()
            await message.answer("Права администратора успешно получены. Команда для входа: /admin")
        else:
            await message.answer("Неверный код.")

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def admin_panel_handler(message: types.Message, state: FSMContext):
    user = get_user(message.from_user.id)
    if not user or user[2] == 0:
        return await message.answer("Недостаточно прав.")
        
    await state.clear()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM keys WHERE is_sold = 0")
        keys_count = cur.fetchone()[0]

    text = (
        "🌿 <b>Панель управления Горошек VPN</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count}</b>\n"
        f"🔑 Свободных ключей: <b>{keys_count}</b>\n\n"
        "Выберите необходимое действие:"
    )
    await message.answer(text, reply_markup=admin_panel_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_cancel")
async def admin_cancel_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("Действие отменено")
    user = get_user(callback.from_user.id)
    if not user or user[2] == 0:
        return await callback.message.edit_text("Действие отменено.")
        
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        users_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM keys WHERE is_sold = 0")
        keys_count = cur.fetchone()[0]

    text = (
        "🌿 <b>Панель управления Горошек VPN</b>\n\n"
        f"👥 Всего пользователей: <b>{users_count}</b>\n"
        f"🔑 Свободных ключей: <b>{keys_count}</b>\n\n"
        "Выберите необходимое действие:"
    )
    await callback.message.edit_text(text, reply_markup=admin_panel_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "adm_broadcast")
async def start_broadcast(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🌿 <b>Рассылка</b>\n\nВведите текст или прикрепите медиа для отправки пользователям:", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.broadcast_text)

@dp.message(AdminStates.broadcast_text)
async def process_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    text_to_send = message.text or message.caption
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        users = cur.fetchall()

    status_msg = await message.answer(f"🌱 Рассылка запущена для {len(users)} пользователей...")
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
            
    await status_msg.edit_text(f"✅ <b>Рассылка завершена.</b>\n\nУспешно доставлено: <b>{success}</b> из <b>{len(users)}</b>", parse_mode="HTML")

@dp.callback_query(F.data == "adm_givemoney")
async def start_give_money(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🌿 <b>Выдача баланса</b>\n\nВведите юзернейм пользователя (например, @username):", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.give_money_user)

@dp.message(AdminStates.give_money_user)
async def process_give_money_user(message: types.Message, state: FSMContext):
    target_user = get_user_by_username(message.text)
    if not target_user:
        return await message.answer("❌ Пользователь не найден. Попробуйте еще раз:", reply_markup=admin_cancel_kb())
    
    await state.update_data(target_id=target_user[0], target_username=message.text)
    await message.answer("🌿 <b>Выдача баланса</b>\n\nВведите сумму для начисления (в рублях):", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.give_money_amount)

@dp.message(AdminStates.give_money_amount)
async def process_give_money_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except ValueError:
        return await message.answer("❌ Введите корректное число:", reply_markup=admin_cancel_kb())
        
    data = await state.get_data()
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, data['target_id']))
        conn.commit()
        
    await state.clear()
    await message.answer(f"✅ Баланс пользователя <b>{data['target_username']}</b> успешно пополнен на <b>{amount:.2f} ₽</b>.", parse_mode="HTML")
    try:
        await bot.send_message(data['target_id'], f"🌿 Ваш баланс был пополнен администратором на <b>{amount:.2f} ₽</b>.", parse_mode="HTML")
    except: pass

@dp.callback_query(F.data == "adm_takemoney")
async def start_take_money(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🌿 <b>Списание баланса</b>\n\nВведите юзернейм пользователя:", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.take_money_user)

@dp.message(AdminStates.take_money_user)
async def process_take_money_user(message: types.Message, state: FSMContext):
    target_user = get_user_by_username(message.text)
    if not target_user:
        return await message.answer("❌ Пользователь не найден:", reply_markup=admin_cancel_kb())
    
    await state.update_data(target_id=target_user[0], target_username=message.text, current_balance=target_user[1])
    await message.answer(f"🌿 Текущий баланс: <b>{target_user[1]:.2f} ₽</b>.\n\nВведите сумму списания:", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.take_money_amount)

@dp.message(AdminStates.take_money_amount)
async def process_take_money_amount(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text)
    except ValueError:
        return await message.answer("❌ Введите число:", reply_markup=admin_cancel_kb())
        
    data = await state.get_data()
    new_balance = max(0.0, data['current_balance'] - amount)
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET balance = ? WHERE user_id = ?", (new_balance, data['target_id']))
        conn.commit()
        
    await state.clear()
    await message.answer(f"✅ Списано <b>{amount:.2f} ₽</b> у <b>{data['target_username']}</b>.\nНовый баланс: <b>{new_balance:.2f} ₽</b>.", parse_mode="HTML")

@dp.callback_query(F.data == "adm_giveadmin")
async def start_give_admin(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🌿 <b>Выдача прав администратора</b>\n\nВведите юзернейм:", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.give_admin_user)

@dp.message(AdminStates.give_admin_user)
async def process_give_admin_user(message: types.Message, state: FSMContext):
    target_user = get_user_by_username(message.text)
    if not target_user:
        return await message.answer("❌ Пользователь не найден:", reply_markup=admin_cancel_kb())
    
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_admin = 1 WHERE user_id = ?", (target_user[0],))
        conn.commit()
        
    await state.clear()
    await message.answer(f"✅ Пользователь <b>{message.text}</b> назначен администратором.", parse_mode="HTML")
    try:
        await bot.send_message(target_user[0], "🛡 Вам были назначены права администратора. Введите /admin")
    except: pass

@dp.callback_query(F.data == "adm_addkey")
async def start_create_key(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🌿 <b>Добавление ключа</b>\n\nВведите срок подписки в месяцах (1, 3, 6 или 12):", reply_markup=admin_cancel_kb(), parse_mode="HTML")
    await state.set_state(AdminStates.create_key_duration)

@dp.message(AdminStates.create_key_duration)
async def process_create_key_duration(message: types.Message, state: FSMContext):
    if message.text not in ["1", "3", "6", "12"]:
        return await message.answer("❌ Доступные варианты срока: 1, 3, 6, 12.", reply_markup=admin_cancel_kb())
    
    await state.update_data(duration=int(message.text))
    await message.answer("🌿 Отправьте текст конфигурационного ключа:", reply_markup=admin_cancel_kb(), parse_mode="HTML")
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
        await message.answer(f"✅ Ключ на <b>{data['duration']} мес.</b> успешно добавлен в базу.", parse_mode="HTML")
    except sqlite3.IntegrityError:
        await message.answer("❌ Такой ключ уже существует в базе данных.")
    finally:
        await state.clear()

# --- КЛИЕНТСКАЯ ЧАСТЬ ---
@dp.message(Command("start"))
async def start_handler(message: types.Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    
    ref_id = None
    if command.args and command.args.isdigit():
        ref_id = int(command.args)
        if ref_id == user_id: 
            ref_id = None
            
    update_user_info(user_id, username, ref_id)
    
    text = (
        "🌱 <b>Добро пожаловать в Горошек VPN</b>\n\n"
        "Ваш надежный проводник в мир быстрого, безопасного и свободного интернета без границ.\n\n"
        "🟢 Высокая скорость без ограничений трафика\n"
        "🟢 Стабильный обход любых блокировок\n"
        "🟢 Мгновенная автоматическая выдача ключа\n\n"
        "Выберите нужный раздел в меню ниже:"
    )
    await message.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "catalog")
async def catalog_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "🌿 <b>Тарифы Горошек VPN</b>\n\n"
        "Выберите подходящий вариант подписки. Доступ к VPN выдается моментально сразу после подтверждения оплаты.\n"
    )
    await callback.message.edit_text(text, reply_markup=catalog_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("select_tarif_"))
async def select_tarif_handler(callback: types.CallbackQuery):
    tarif_id = callback.data.split("_")[2]
    tarif = TARIFS.get(tarif_id)
    if not tarif: return
        
    text = (
        f"🌿 <b>Подтверждение заказа</b>\n\n"
        f"📦 Тариф: <b>{tarif['name']}</b>\n"
        f"💰 Стоимость: <b>{tarif['price']:.2f} ₽</b>\n\n"
        f"Сумма будет списана с вашего внутреннего баланса в боте."
    )
    await callback.message.edit_text(text, reply_markup=confirm_kb(tarif_id), parse_mode="HTML")

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
            return await callback.answer("⚠️ Ключи для выбранного тарифа временно закончились. Скоро будет пополнение!", show_alert=True)
            
        if user[0] < tarif["price"]:
            return await callback.answer("⚠️ Недостаточно средств на балансе. Пожалуйста, пополните счет.", show_alert=True)
        
        now = datetime.now()
        current_sub_expires = user[3]
        if current_sub_expires:
            try:
                exp_date = datetime.fromisoformat(current_sub_expires)
                base_date = exp_date if exp_date > now else now
            except:
                base_date = now
        else:
            base_date = now
            
        new_expires = base_date + timedelta(days=tarif["months"] * 30)
        new_expires_str = new_expires.isoformat()

        cur.execute("UPDATE users SET balance = balance - ?, sub_expires = ?, key_data = ? WHERE user_id = ?", 
                    (tarif["price"], new_expires_str, key[1], user_id))
        cur.execute("UPDATE keys SET is_sold = 1 WHERE id = ?", (key[0],))
        
        if user[1]:
            bonus_amount = round(tarif["price"] * REF_PERCENT, 2)
            cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bonus_amount, user[1]))
            try: 
                await bot.send_message(user[1], f"🍀 <b>Реферальный бонус</b>\n\nВаш друг активировал подписку в боте.\nВам зачислено на баланс: <b>{bonus_amount:.2f} ₽</b>.", parse_mode="HTML")
            except: pass
        conn.commit()
    
    text = (
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"Ваш ключ доступа:\n"
        f"<code>{key[1]}</code>\n\n"
        f"{HAPP_INSTRUCTION}"
    )
    await callback.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "referral_menu")
async def referral_menu_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    ref_count = get_referrals_count(user_id)
    bot_info = await bot.me()
    ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
    
    text = (
        "🍀 <b>Реферальная программа</b>\n\n"
        "Приглашайте друзей в Горошек VPN и получайте <b>10%</b> от каждой их покупки на свой внутренний баланс.\n\n"
        f"👥 Приглашено друзей: <b>{ref_count}</b>\n\n"
        "Ваша персональная ссылка для приглашений:\n"
        f"<code>{ref_link}</code>"
    )
    await callback.message.edit_text(text, reply_markup=referral_kb(), parse_mode="HTML")

# --- ПОПОЛНЕНИЕ БАЛАНСА ---
@dp.callback_query(F.data == "topup_menu")
async def topup_menu_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "💳 <b>Пополнение баланса</b>\n\nВыберите способ пополнения:"
    await callback.message.edit_text(text, reply_markup=topup_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "topup_platega_menu")
async def topup_platega_menu_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "💳 <b>Пополнение через Platega (Рубли)</b>\n\nВыберите готовую сумму пополнения или введите свою:"
    await callback.message.edit_text(text, reply_markup=topup_platega_amounts_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("platega_pay_"))
async def platega_fixed_pay(callback: types.CallbackQuery):
    amount = float(callback.data.split("_")[2])
    await process_platega_generation(callback, amount)

@dp.callback_query(F.data == "platega_custom_amount")
async def platega_custom_amount_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "✍️ <b>Пополнение баланса</b>\n\nВведите сумму для пополнения в рублях (например, <i>150</i> или <i>500</i>):",
        reply_markup=cancel_topup_kb(),
        parse_mode="HTML"
    )
    await state.set_state(TopUpStates.custom_amount)

@dp.message(TopUpStates.custom_amount)
async def platega_custom_amount_process(message: types.Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        return await message.answer("❌ Неверный формат суммы. Введите корректное число (например, 200):", reply_markup=cancel_topup_kb())
    
    await state.clear()
    
    # Создаем фейковый callback_query для универсальной отправки ссылки
    class PseudoCallback:
        def __init__(self, msg):
            self.message = msg
            self.from_user = msg.from_user
        async def answer(self, *args, **kwargs):
            pass

    await process_platega_generation(PseudoCallback(message), amount)

async def process_platega_generation(callback, amount: float):
    user_id = callback.from_user.id
    username = callback.from_user.username
    
    wait_msg = await callback.message.answer("⏳ Создаем ссылку на оплату...")
    payment_url = await create_platega_payment(amount, user_id, username)
    
    if not payment_url:
        return await wait_msg.edit_text("❌ Ошибка создания платежа через Platega. Попробуйте позже.", reply_markup=topup_platega_amounts_kb())
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔗 Оплатить {amount:.2f} ₽", url=payment_url)],
        [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="profile")]
    ])
    
    await wait_msg.edit_text(
        f"💳 <b>Счет на оплату создан!</b>\n\n"
        f"Сумма: <b>{amount:.2f} ₽</b>\n\n"
        f"Нажмите кнопку ниже для перехода к оплате. После успешного платежа средства зачислятся автоматически.",
        reply_markup=kb,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "topup_stars_menu")
async def topup_stars_menu_handler(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = "⭐️ <b>Пополнение через Telegram Stars</b>\n\nВыберите удобную сумму (1 звезда = 1 рубль):"
    await callback.message.edit_text(text, reply_markup=topup_stars_amounts_kb(), parse_mode="HTML")

@dp.callback_query(F.data.startswith("paystars_"))
async def pay_stars_handler(callback: types.CallbackQuery):
    amount = int(callback.data.split("_")[1])
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Пополнение баланса",
        description=f"Пополнение счета Горошек VPN на {amount} ₽",
        provider_token="", 
        currency="XTR",
        prices=[LabeledPrice(label="Пополнение баланса", amount=amount)],
        start_parameter="topup",
        payload=f"stars_{amount}"
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    amount = float(message.successful_payment.total_amount)
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, message.from_user.id))
        conn.commit()
    await message.answer(f"✅ <b>Баланс успешно пополнен на {amount:.2f} ₽!</b>", reply_markup=main_menu_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "show_my_key")
async def show_my_key_handler(callback: types.CallbackQuery):
    user = get_user(callback.from_user.id)
    key_data = user[4]
    if not key_data:
        return await callback.answer("⚠️ У вас нет активного ключа.", show_alert=True)
    
    text = (
        f"🔑 <b>Ваш ключ доступа:</b>\n"
        f"<code>{key_data}</code>\n\n"
        f"{HAPP_INSTRUCTION}"
    )
    await callback.message.edit_text(text, reply_markup=profile_kb(True), parse_mode="HTML")

@dp.message(Command("profile"))
@dp.callback_query(F.data == "profile")
async def profile_handler(event: types.Message | types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    user_id = event.from_user.id
    user = get_user(user_id)
    balance = user[0]
    sub_expires_str = user[3]
    key_data = user[4]
    
    sub_status = "❌ Не активна"
    days_left = 0
    has_sub = False
    
    if sub_expires_str and key_data:
        try:
            exp_date = datetime.fromisoformat(sub_expires_str)
            now = datetime.now()
            if exp_date > now:
                delta = exp_date - now
                days_left = delta.days + (1 if delta.seconds > 0 else 0)
                sub_status = f"🟢 Активна"
                has_sub = True
        except:
            pass

    text = (
        f"👤 <b>Личный кабинет</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"💳 <b>Баланс:</b> <b>{balance:.2f} ₽</b>\n\n"
        f"🛡 <b>Статус подписки:</b> {sub_status}\n"
        f"⏳ <b>Осталось дней:</b> <b>{days_left}</b>\n"
    )
    
    kb = profile_kb(has_sub)
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await event.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("help"))
@dp.callback_query(F.data == "help")
async def help_handler(event: types.Message | types.CallbackQuery, state: FSMContext = None):
    if state:
        await state.clear()
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(HAPP_INSTRUCTION, reply_markup=back_kb(), parse_mode="HTML")
    else:
        await event.answer(HAPP_INSTRUCTION, reply_markup=back_kb(), parse_mode="HTML")

@dp.callback_query(F.data == "back_main")
async def back_main(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        "🌱 <b>Добро пожаловать в Горошек VPN</b>\n\n"
        "Ваш надежный проводник в мир быстрого, безопасного и свободного интернета без границ.\n\n"
        "🟢 Высокая скорость без ограничений трафика\n"
        "🟢 Стабильный обход любых блокировок\n"
        "🟢 Мгновенная автоматическая выдача ключа\n\n"
        "Выберите нужный раздел в меню ниже:"
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")

# --- WEB СЕРВЕР RENDER (ВКЛЮЧАЯ WEBHOOK PLATEGA) ---
async def handle_ping(request):
    return web.Response(text="Goroshek VPN is running.")

async def handle_platega_webhook(request):
    try:
        data = await request.json()
        
        # Проверяем структуру ответа от Platega
        status = data.get("status")
        metadata = data.get("metadata", {})
        telegram_id = metadata.get("telegram_id")
        amount = metadata.get("amount")
        
        if (status == "SUCCESS" or status == "paid") and telegram_id and amount:
            with sqlite3.connect(DB_FILE) as conn:
                cur = conn.cursor()
                cur.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (float(amount), int(telegram_id)))
                conn.commit()
                
            try:
                await bot.send_message(
                    int(telegram_id),
                    f"✅ <b>Оплата прошла успешно!</b>\n\nВаш баланс пополнен на <b>{float(amount):.2f} ₽</b>.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Failed to send success message to user {telegram_id}: {e}")
                
        return web.json_response({"status": "ok"})
    except Exception as e:
        logging.error(f"Webhook error: {e}")
        return web.json_response({"status": "error", "message": str(e)}, status=400)

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
    
    await bot.delete_webhook(drop_pending_updates=True)
    await set_my_commands(bot)
    
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_post("/platega/webhook", handle_platega_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    asyncio.create_task(self_ping())
    await dp.start_polling(bot, hold_as_tasks=True) # type: ignore

if __name__ == "__main__":
    asyncio.run(main())
