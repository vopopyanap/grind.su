import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List
from urllib.parse import quote, unquote

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, PreCheckoutQuery, Message,
    CallbackQuery, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
import yookassa
from yookassa import Payment, Configuration

# ---------- Загрузка .env ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")

# ---------- Проверка обязательных переменных ----------
if not BOT_TOKEN or not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
    raise EnvironmentError("Не заполнены обязательные переменные в .env (BOT_TOKEN, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)")

# ---------- Абсолютный путь к папке скрипта ----------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- Настройка YooKassa ----------
Configuration.configure(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------- База данных JSON ----------
DB_FILE = os.path.join(SCRIPT_DIR, "data.json")

def load_db() -> Dict[str, Any]:
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        logger.error("База данных повреждена или отсутствует. Создаю новую.")
        default_db = {
            "users": {},
            "promocodes": {
                "ПРЯНЯ": {
                    "code": "ПРЯНЯ",
                    "discount": 50,
                    "max_uses": None,
                    "uses": 0,
                    "created_by": 0,
                    "created_at": "2026-06-20T00:00:00"
                }
            },
            "stats": {
                "total_users": 0,
                "total_purchases": 0,
                "total_stars_purchases": 0,
                "total_yoomoney_purchases": 0
            }
        }
        save_db(default_db)
        return default_db

def save_db(data: Dict[str, Any]):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# ---------- Инициализация бота и диспетчера ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ---------- Состояния FSM ----------
class BuyState(StatesGroup):
    waiting_for_promo = State()

class AdminAction(StatesGroup):
    creating_promo = State()
    deleting_promo = State()
    adding_balance = State()
    blocking_user = State()
    unblocking_user = State()
    granting_access = State()
    # новые состояния
    adding_stars = State()
    sending_gift = State()

class SupportState(StatesGroup):
    in_support = State()

# ---------- Inline-клавиатура главного меню ----------
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🛒 Купить VPN", callback_data="menu_buy"),
        InlineKeyboardButton(text="👤 Профиль", callback_data="menu_profile")
    )
    builder.row(
        InlineKeyboardButton(text="👥 Рефералы", callback_data="menu_referral"),
        InlineKeyboardButton(text="💬 Поддержка", callback_data="menu_support")
    )
    return builder.as_markup()

# ---------- Вспомогательные функции ----------
def get_user(db, user_id: int):
    return db["users"].get(str(user_id))

def ensure_user(db, user_id: int, username: str, first_name: str, referrer_id: int = None):
    user = get_user(db, user_id)
    if not user:
        now = datetime.now(timezone.utc).isoformat()
        user = {
            "id": user_id,
            "username": username,
            "first_name": first_name,
            "referrer_id": referrer_id,
            "balance": 0.0,
            "purchased": False,
            "purchase_date": None,
            "purchase_method": None,
            "promo_used": None,
            "blocked": False,
            "registered_at": now
        }
        db["users"][str(user_id)] = user
        db["stats"]["total_users"] += 1
        save_db(db)
        return user, True
    return user, False

def apply_promo(db, code: str):
    code = code.strip().upper()
    promo = db["promocodes"].get(code)
    if not promo:
        return None, "❌ Промокод не найден"
    if promo["max_uses"] is not None and promo["uses"] >= promo["max_uses"]:
        return None, "❌ Промокод больше не действителен"
    return promo, None

def can_purchase(user):
    if user["blocked"]:
        return False, "❌ Вы заблокированы."
    if user["purchased"]:
        return False, "❌ Вы уже приобрели ПРЯНЯ ВПН."
    return True, None

async def create_yoomoney_payment(amount_rub: float, description: str, user_id: int, promo_code: str = None, payment_method: str = None):
    idempotence_key = str(uuid.uuid4())
    try:
        data = {
            "amount": {
                "value": f"{amount_rub:.2f}",
                "currency": "RUB"
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/your_bot"
            },
            "description": description,
            "capture": True,
            "metadata": {
                "user_id": user_id,
                "promo_code": promo_code or ""
            }
        }
        if payment_method == "sbp":
            data["payment_method_data"] = {"type": "sbp"}
        payment = Payment.create(data, idempotence_key)
        return payment.confirmation.confirmation_url, payment.id
    except Exception as e:
        logger.error(f"Ошибка создания платежа: {e}")
        return None, None

async def complete_purchase(user_id: int, method: str, promo_code: str = None):
    db = load_db()
    user = get_user(db, user_id)
    if not user or user["purchased"]:
        return False

    used_promo = None
    final_price = 200.0
    if method == "yoomoney" and promo_code:
        promo_info, _ = apply_promo(db, promo_code)
        if promo_info:
            final_price = 200.0 - promo_info["discount"]
            used_promo = promo_code
            promo_info["uses"] += 1
            db["promocodes"][promo_code] = promo_info

    now = datetime.now(timezone.utc).isoformat()
    user["purchased"] = True
    user["purchase_date"] = now
    user["purchase_method"] = method
    user["promo_used"] = used_promo

    db["stats"]["total_purchases"] += 1
    if method == "stars":
        db["stats"]["total_stars_purchases"] += 1
    elif method == "yoomoney":
        db["stats"]["total_yoomoney_purchases"] += 1
    elif method == "balance":
        db["stats"]["total_yoomoney_purchases"] += 1

    # Реферальные начисления
    if user["referrer_id"]:
        referrer = get_user(db, user["referrer_id"])
        if referrer:
            referrer["balance"] += 20.0
            db["users"][str(user["referrer_id"])] = referrer
            try:
                await bot.send_message(
                    chat_id=user["referrer_id"],
                    text="🎉 Ваш друг купил ПРЯНЯ ВПН! Вам начислено 20 руб. на баланс."
                )
            except Exception as e:
                logger.warning(f"Не удалось уведомить реферера {user['referrer_id']}: {e}")
    if method != "balance":
        user["balance"] += 20.0

    db["users"][str(user_id)] = user
    save_db(db)

    # Отправка QR и инструкции
    try:
        instructions_path = os.path.join(SCRIPT_DIR, "instructions.txt")
        qr_path = os.path.join(SCRIPT_DIR, "qr.png")
        if not os.path.exists(instructions_path) or not os.path.exists(qr_path):
            raise FileNotFoundError(f"Файлы не найдены:\n{instructions_path}\n{qr_path}")
        with open(instructions_path, "r", encoding="utf-8") as f:
            instructions = f.read()
        photo = FSInputFile(qr_path)
        await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=f"🎉 Готово! Ты в деле с ПРЯНЯ ВПН!\n\n{instructions}"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки файлов пользователю {user_id}: {e}")
        await bot.send_message(
            chat_id=user_id,
            text=f"⚠️ Не удалось отправить QR-код и инструкцию.\nОшибка: {e}\nПожалуйста, свяжитесь с поддержкой /support"
        )

    # Уведомление админам
    admin_msg = (
        f"💰 Новая покупка!\n"
        f"👤 {user['first_name']} (ID: {user_id})"
        f"{' @' + user['username'] if user['username'] else ''}\n"
        f"Способ: {method}\n"
        f"Промокод: {used_promo or 'нет'}\n"
        f"Реферал от: {user['referrer_id'] or 'нет'}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_msg)
        except Exception as e:
            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")

    return True

# ---------- Команда /start ----------
@dp.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    db = load_db()
    args = message.text.split()
    referrer_id = None
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            decoded = unquote(args[1])
            referrer_id = int(decoded[3:])
        except (ValueError, IndexError):
            pass

    user, is_new = ensure_user(db, user_id, username, first_name, referrer_id)
    if user["blocked"]:
        await message.answer("❌ Вы заблокированы.", reply_markup=main_menu_keyboard())
        return

    welcome = (
        f"👋 Хей, {first_name}! Добро пожаловать в ПРЯНЯ ВПН 🛡️\n"
        "Быстрый, вечный и надёжный VPN навсегда!\n\n"
        "💰 Цена: 200 руб. (или 300 Telegram Stars)\n"
        "🎁 Рефералы: +20 руб. вам и другу\n"
        "🏷️ Промокоды: ищи скидки!\n\n"
        "Выбирай действие 👇"
    )
    if is_new and referrer_id:
        welcome += "\n🔗 Вы пришли по реферальной ссылке. После первой покупки оба получите бонусы!"
    await message.answer(welcome, reply_markup=main_menu_keyboard())

    if is_new:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(chat_id=admin_id, text=f"🆕 Новый пользователь: {first_name} (ID: {user_id})")
            except:
                pass

# ---------- Обработчики Inline-кнопок главного меню ----------
@dp.callback_query(F.data == "menu_buy")
async def menu_buy(callback: CallbackQuery, state: FSMContext):
    db = load_db()
    user = get_user(db, callback.from_user.id)
    if not user:
        await callback.message.edit_text("Сначала нажмите /start", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    ok, error = can_purchase(user)
    if not ok:
        await callback.message.edit_text(error, reply_markup=main_menu_keyboard())
        await callback.answer()
        return

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 ЮMoney (200 руб.)", callback_data="pay_yoomoney"))
    builder.row(InlineKeyboardButton(text="⭐ Telegram Stars (300 звёзд)", callback_data="pay_stars"))
    if user["balance"] >= 200:
        builder.row(InlineKeyboardButton(text="💰 Оплатить с баланса (200 руб.)", callback_data="pay_balance"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back"))
    await callback.message.edit_text(
        "🛒 Выбери способ оплаты ПРЯНЯ ВПН:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_profile")
async def menu_profile(callback: CallbackQuery):
    db = load_db()
    user = get_user(db, callback.from_user.id)
    if not user:
        await callback.message.edit_text("Сначала нажмите /start", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    status = "✅ Куплен" if user["purchased"] else "❌ Не куплен"
    me = await bot.get_me()
    payload = quote(f"ref{user['id']}", safe='')
    ref_link = f"https://t.me/{me.username}?start={payload}"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Моя реферальная ссылка", url=ref_link))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back"))
    await callback.message.edit_text(
        f"👤 Твой профиль:\n"
        f"Имя: {user['first_name']}\n"
        f"ID: {user['id']}\n"
        f"💰 Баланс: {user['balance']:.2f} руб.\n"
        f"Статус VPN: {status}\n"
        f"📅 Регистрация: {user['registered_at'][:10]}\n\n"
        f"Нажми кнопку ниже, чтобы скопировать реферальную ссылку 👇",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_referral")
async def menu_referral(callback: CallbackQuery):
    user_id = callback.from_user.id
    me = await bot.get_me()
    payload = quote(f"ref{user_id}", safe='')
    ref_link = f"https://t.me/{me.username}?start={payload}"
    db = load_db()
    count = sum(1 for u in db["users"].values() if u.get("referrer_id") == user_id)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔗 Моя реферальная ссылка", url=ref_link))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_back"))
    await callback.message.edit_text(
        f"👥 Партнёрская программа:\n\n"
        f"Приглашено друзей: {count}\n"
        f"Ты получишь 20 руб. на баланс после первой покупки друга!\n\n"
        f"Скопируй ссылку по кнопке ниже или отправь другу вручную 🚀",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "menu_support")
async def menu_support(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SupportState.in_support)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Выйти из поддержки", callback_data="exit_support"))
    await callback.message.edit_text(
        "📞 Ты в режиме поддержки. Просто напиши вопрос, и мы ответим!\n"
        "Для выхода нажми кнопку ниже или введи /stop_support.",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "exit_support", StateFilter(SupportState.in_support))
async def exit_support_button(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Вышел из режима поддержки 👋", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.message(Command("stop_support"), StateFilter(SupportState.in_support))
async def cmd_stop_support(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Вышел из режима поддержки 👋", reply_markup=main_menu_keyboard())

@dp.message(StateFilter(SupportState.in_support))
async def support_forward(message: Message):
    for admin_id in ADMIN_IDS:
        try:
            await message.forward(chat_id=admin_id)
            await message.answer("✅ Твоё сообщение отправлено администратору, жди ответа!")
        except Exception as e:
            logger.error(f"Support forward error: {e}")

@dp.callback_query(F.data == "menu_back")
async def menu_back(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

# ---------- Колбэки оплаты ----------
@dp.callback_query(F.data == "pay_yoomoney")
async def process_pay_yoomoney(callback: CallbackQuery, state: FSMContext):
    db = load_db()
    user = get_user(db, callback.from_user.id)
    ok, error = can_purchase(user)
    if not ok:
        await callback.message.edit_text(error, reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    await state.update_data(payment_method="yoomoney")
    await state.set_state(BuyState.waiting_for_promo)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🏷️ Пропустить промокод", callback_data="skip_promo"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_buy"))
    await callback.message.edit_text(
        "🏷️ Введи промокод (если есть) или пропусти:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "skip_promo")
async def process_skip_promo(callback: CallbackQuery, state: FSMContext):
    await state.update_data(promo_code=None)
    await start_yoomoney_payment(callback.message, callback.from_user.id, None, state)
    await callback.answer()

@dp.callback_query(F.data == "pay_stars")
async def process_pay_stars(callback: CallbackQuery, state: FSMContext):
    db = load_db()
    user = get_user(db, callback.from_user.id)
    ok, error = can_purchase(user)
    if not ok:
        await callback.message.edit_text(error, reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    await callback.message.edit_text("⭐ Формируем счёт...")
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="ПРЯНЯ ВПН навсегда",
        description="Быстрый и вечный VPN. Спасибо за поддержку!",
        payload="vpn_purchase",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="ПРЯНЯ ВПН", amount=300)],
        start_parameter="vpn_buy"
    )
    await callback.answer()

@dp.callback_query(F.data == "pay_balance")
async def process_pay_balance(callback: CallbackQuery):
    user_id = callback.from_user.id
    db = load_db()
    user = get_user(db, user_id)
    ok, error = can_purchase(user)
    if not ok:
        await callback.message.edit_text(error, reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    if user["balance"] < 200:
        await callback.message.edit_text("❌ Недостаточно средств на балансе.", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    user["balance"] -= 200
    db["users"][str(user_id)] = user
    save_db(db)
    await complete_purchase(user_id, method="balance")
    await callback.message.edit_text("✅ Оплата с баланса прошла успешно! Инструкция ниже 👇", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.message(StateFilter(BuyState.waiting_for_promo))
async def promo_input(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    db = load_db()
    promo_info, err = apply_promo(db, code)
    if promo_info:
        await message.answer(f"🎉 Промокод **{code}** применён! Скидка {promo_info['discount']} руб.")
        await state.update_data(promo_code=code)
        await start_yoomoney_payment(message, message.from_user.id, code, state)
    else:
        await message.answer(f"❌ {err}. Попробуй другой или вернись в меню /buy")
        await state.set_state(BuyState.waiting_for_promo)

async def start_yoomoney_payment(message: Message, user_id: int, promo_code: str | None, state: FSMContext):
    db = load_db()
    final_price = 200.0
    if promo_code:
        promo_info, _ = apply_promo(db, promo_code)
        if promo_info:
            final_price = 200.0 - promo_info["discount"]

    # Создаём платёж без СБП по умолчанию (можно добавить кнопку СБП отдельно)
    confirmation_url, payment_id = await create_yoomoney_payment(
        final_price, "ПРЯНЯ ВПН навсегда", user_id, promo_code
    )
    if not confirmation_url:
        await message.answer("❌ Ошибка создания платежа. Попробуй позже.")
        return
    await state.update_data(pending_payment_id=payment_id, pending_promo=promo_code)
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💳 Перейти к оплате", url=confirmation_url))
    builder.row(InlineKeyboardButton(text="✅ Я оплатил", callback_data="check_yoomoney"))
    await message.answer(
        f"💳 Сумма к оплате: **{final_price:.2f} руб.**\n"
        "👉 Нажми «Перейти к оплате», оплати, и затем нажми «Я оплатил».",
        reply_markup=builder.as_markup()
    )
    await state.set_state(None)

@dp.callback_query(F.data == "check_yoomoney")
async def check_payment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payment_id = data.get("pending_payment_id")
    if not payment_id:
        await callback.message.edit_text("❌ Платёж не найден. Начни заново /buy", reply_markup=main_menu_keyboard())
        await callback.answer()
        return
    try:
        payment = Payment.find_one(payment_id)
        if payment.status == "succeeded":
            promo = data.get("pending_promo")
            await complete_purchase(callback.from_user.id, method="yoomoney", promo_code=promo)
            await callback.message.edit_text("✅ Оплата прошла! Инструкция и QR ниже 👇", reply_markup=main_menu_keyboard())
            await state.clear()
        else:
            builder = InlineKeyboardBuilder()
            builder.row(InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data="check_yoomoney"))
            await callback.message.edit_text(
                "⌛ Оплата ещё не прошла. Проверь ещё раз после оплаты.",
                reply_markup=builder.as_markup()
            )
    except Exception as e:
        logger.error(f"Ошибка проверки: {e}")
        await callback.message.edit_text("❌ Ошибка при проверке платежа.", reply_markup=main_menu_keyboard())
    await callback.answer()

# ---------- Обработчики платежей (звёзды и пополнение) ----------
@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_query: PreCheckoutQuery):
    user_id = pre_checkout_query.from_user.id
    payload = pre_checkout_query.invoice_payload

    # Пополнение Stars для админа
    if payload == "admin_stars_topup":
        if user_id in ADMIN_IDS:
            await pre_checkout_query.answer(ok=True)
        else:
            await pre_checkout_query.answer(ok=False, error_message="Недостаточно прав.")
        return

    # Стандартная покупка VPN
    db = load_db()
    user = get_user(db, user_id)
    if user and not user["blocked"] and not user["purchased"]:
        await pre_checkout_query.answer(ok=True)
    else:
        await pre_checkout_query.answer(ok=False, error_message="Вы не можете совершить эту покупку.")

@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload

    if payload == "admin_stars_topup":
        # Пополнение звёзд для бота (админ)
        amount = message.successful_payment.total_amount  # в XTR (1 звезда = 1 единица)
        await message.answer(
            f"✅ Stars успешно пополнены!\n"
            f"⭐ Количество: {amount}\n"
            f"💰 Платеж успешно зачислен.",
            reply_markup=admin_panel_keyboard() if user_id in ADMIN_IDS else main_menu_keyboard()
        )
        return

    # Стандартная покупка VPN
    if payload == "vpn_purchase":
        await complete_purchase(user_id, method="stars")
        await message.answer(
            "⭐ Спасибо за покупку через Telegram Stars! Ты просто космос 🚀",
            reply_markup=main_menu_keyboard()
        )
    else:
        # На случай неизвестного payload
        await message.answer("Неизвестный тип платежа.", reply_markup=main_menu_keyboard())

# ---------- Админ-панель (Inline) ----------
def is_admin(callback_or_message) -> bool:
    if isinstance(callback_or_message, CallbackQuery):
        return callback_or_message.from_user.id in ADMIN_IDS
    elif isinstance(callback_or_message, Message):
        return callback_or_message.from_user.id in ADMIN_IDS
    return False

def admin_panel_keyboard():
    builder = InlineKeyboardBuilder()
    buttons = [
        ("📊 Статистика", "admin_stats"),
        ("👥 Пользователи", "admin_users"),
        ("🎫 Создать промокод", "admin_create_promo"),
        ("🗑 Удалить промокод", "admin_delete_promo"),
        ("💰 Пополнить баланс", "admin_add_balance"),
        ("🚫 Заблокировать", "admin_block"),
        ("✅ Разблокировать", "admin_unblock"),
        ("🎁 Выдать доступ", "admin_grant"),
        ("📋 Список промокодов", "admin_promos"),
        # новые кнопки
        ("⭐ Пополнить Stars", "admin_add_stars"),
        ("🎁 Отправить подарок", "admin_send_gift"),
    ]
    for text, data in buttons:
        builder.row(InlineKeyboardButton(text=text, callback_data=data))
    builder.row(InlineKeyboardButton(text="🔙 Выйти из админки", callback_data="menu_back"))
    return builder.as_markup()

@dp.message(Command("admin"), is_admin)
async def cmd_admin(message: Message):
    await message.answer("🔐 Админ-панель:", reply_markup=admin_panel_keyboard())

@dp.callback_query(lambda c: c.data and c.data.startswith("admin_"), is_admin)
async def admin_callback_handler(callback: CallbackQuery, state: FSMContext):
    action = callback.data
    if action == "admin_stats":
        db = load_db()
        stats = db["stats"]
        await callback.message.edit_text(
            f"📊 Статистика:\n"
            f"👥 Пользователей: {stats['total_users']}\n"
            f"💰 Всего покупок: {stats['total_purchases']}\n"
            f"⭐ Звёзды: {stats['total_stars_purchases']}\n"
            f"💳 ЮMoney: {stats['total_yoomoney_purchases']}",
            reply_markup=admin_panel_keyboard()
        )
    elif action == "admin_users":
        db = load_db()
        users = db["users"]
        if not users:
            text = "Пусто."
        else:
            lines = []
            for uid, u in list(users.items())[:30]:
                status = "✅" if u["purchased"] else "❌"
                block = "🚫" if u["blocked"] else ""
                lines.append(f"{u['first_name']} (ID:{uid}) {status} {block}")
            text = "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    elif action == "admin_promos":
        db = load_db()
        promos = db["promocodes"]
        if not promos:
            text = "Нет промокодов."
        else:
            text = ""
            for code, p in promos.items():
                text += f"🏷️ {code}: скидка {p['discount']} руб., исп. {p['uses']}\n"
        await callback.message.edit_text(text, reply_markup=admin_panel_keyboard())
    elif action == "admin_create_promo":
        await state.set_state(AdminAction.creating_promo)
        await callback.message.edit_text(
            "Введи: КОД СКИДКА МАКС_ИСП (через пробел). Пример: SALE 30 100",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="Отмена", callback_data="admin_cancel")).as_markup()
        )
    elif action == "admin_delete_promo":
        await state.set_state(AdminAction.deleting_promo)
        await callback.message.edit_text(
            "Введи код промокода:",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="Отмена", callback_data="admin_cancel")).as_markup()
        )
    elif action == "admin_add_balance":
        await state.set_state(AdminAction.adding_balance)
        await callback.message.edit_text(
            "Введи: ID_пользователя СУММА (пример: 12345678 50)",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="Отмена", callback_data="admin_cancel")).as_markup()
        )
    elif action == "admin_block":
        await state.set_state(AdminAction.blocking_user)
        await callback.message.edit_text(
            "Введи ID пользователя:",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="Отмена", callback_data="admin_cancel")).as_markup()
        )
    elif action == "admin_unblock":
        await state.set_state(AdminAction.unblocking_user)
        await callback.message.edit_text(
            "Введи ID пользователя:",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="Отмена", callback_data="admin_cancel")).as_markup()
        )
    elif action == "admin_grant":
        await state.set_state(AdminAction.granting_access)
        await callback.message.edit_text(
            "Введи ID пользователя:",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="Отмена", callback_data="admin_cancel")).as_markup()
        )
    # НОВЫЕ ДЕЙСТВИЯ
    elif action == "admin_add_stars":
        await state.set_state(AdminAction.adding_stars)
        await callback.message.edit_text(
            "Введи количество Stars для пополнения (целое число):",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")).as_markup()
        )
    elif action == "admin_send_gift":
        try:
            # Получаем список доступных подарков
            gifts = await bot.get_available_gifts()
            if not gifts or not gifts.gifts:
                await callback.message.edit_text(
                    "❌ Нет доступных подарков. Возможно, бот не имеет прав.",
                    reply_markup=admin_panel_keyboard()
                )
                await callback.answer()
                return

            # Формируем сообщение со списком
            gift_list = format_gift_list(gifts.gifts)
            # Сохраняем список в state для возможного использования (необязательно)
            await state.update_data(gift_list=gifts.gifts)
            await state.set_state(AdminAction.sending_gift)

            await callback.message.edit_text(
                f"🎁 Доступные подарки:\n\n{gift_list}\n\n"
                "Введи данные в формате:\n"
                "АЙДИ_ПОДАРКА.АЙДИ_ПОЛУЧАТЕЛЯ.ТЕКСТ\n\n"
                "Пример: 5170233101234567890.123456789.Привет!",
                reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")).as_markup()
            )
        except Exception as e:
            logger.error(f"Ошибка получения подарков: {e}")
            await callback.message.edit_text(
                f"❌ Не удалось получить список подарков:\n{e}",
                reply_markup=admin_panel_keyboard()
            )
    await callback.answer()

@dp.callback_query(F.data == "admin_cancel", is_admin)
async def admin_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Действие отменено.", reply_markup=admin_panel_keyboard())
    await callback.answer()

# Обработка ввода данных для админских действий
@dp.message(StateFilter(AdminAction.creating_promo), is_admin)
async def admin_create_promo_finish(message: Message, state: FSMContext):
    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("Формат: КОД СКИДКА [МАКС_ИСП]")
        return
    code = parts[0].upper()
    discount = float(parts[1])
    max_uses = int(parts[2]) if len(parts) > 2 else None
    db = load_db()
    db["promocodes"][code] = {
        "code": code,
        "discount": discount,
        "max_uses": max_uses,
        "uses": 0,
        "created_by": message.from_user.id,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_db(db)
    await message.answer(f"✅ Промокод **{code}** создан!", reply_markup=admin_panel_keyboard())
    await state.clear()

@dp.message(StateFilter(AdminAction.deleting_promo), is_admin)
async def admin_delete_promo_finish(message: Message, state: FSMContext):
    code = message.text.strip().upper()
    db = load_db()
    if code in db["promocodes"]:
        del db["promocodes"][code]
        save_db(db)
        await message.answer(f"🗑 Промокод {code} удалён.", reply_markup=admin_panel_keyboard())
    else:
        await message.answer("❌ Не найден.", reply_markup=admin_panel_keyboard())
    await state.clear()

@dp.message(StateFilter(AdminAction.adding_balance), is_admin)
async def admin_add_balance_finish(message: Message, state: FSMContext):
    try:
        parts = message.text.strip().split()
        target_id = int(parts[0])
        amount = float(parts[1])
        db = load_db()
        user = get_user(db, target_id)
        if user:
            user["balance"] += amount
            db["users"][str(target_id)] = user
            save_db(db)
            await message.answer(f"💰 Баланс {target_id} пополнен на {amount} руб.", reply_markup=admin_panel_keyboard())
        else:
            await message.answer("❌ Пользователь не найден.", reply_markup=admin_panel_keyboard())
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}", reply_markup=admin_panel_keyboard())
    await state.clear()

@dp.message(StateFilter(AdminAction.blocking_user), is_admin)
async def admin_block_user_finish(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        db = load_db()
        user = get_user(db, target_id)
        if user:
            user["blocked"] = True
            db["users"][str(target_id)] = user
            save_db(db)
            await message.answer(f"🚫 Пользователь {target_id} заблокирован.", reply_markup=admin_panel_keyboard())
        else:
            await message.answer("❌ Не найден.", reply_markup=admin_panel_keyboard())
    except ValueError:
        await message.answer("❌ Некорректный ID.", reply_markup=admin_panel_keyboard())
    await state.clear()

@dp.message(StateFilter(AdminAction.unblocking_user), is_admin)
async def admin_unblock_user_finish(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        db = load_db()
        user = get_user(db, target_id)
        if user:
            user["blocked"] = False
            db["users"][str(target_id)] = user
            save_db(db)
            await message.answer(f"✅ Пользователь {target_id} разблокирован.", reply_markup=admin_panel_keyboard())
        else:
            await message.answer("❌ Не найден.", reply_markup=admin_panel_keyboard())
    except ValueError:
        await message.answer("❌ Некорректный ID.", reply_markup=admin_panel_keyboard())
    await state.clear()

@dp.message(StateFilter(AdminAction.granting_access), is_admin)
async def admin_grant_access_finish(message: Message, state: FSMContext):
    try:
        target_id = int(message.text.strip())
        db = load_db()
        user = get_user(db, target_id)
        if user and not user["purchased"]:
            await complete_purchase(target_id, method="manual_admin")
            await message.answer(f"🎁 Доступ выдан пользователю {target_id}.", reply_markup=admin_panel_keyboard())
        else:
            await message.answer("❌ Не найден или уже куплен.", reply_markup=admin_panel_keyboard())
    except ValueError:
        await message.answer("❌ Некорректный ID.", reply_markup=admin_panel_keyboard())
    await state.clear()

# ---------- НОВЫЕ ОБРАБОТЧИКИ ДЛЯ ПОПОЛНЕНИЯ STARS И ОТПРАВКИ ПОДАРКА ----------

@dp.message(StateFilter(AdminAction.adding_stars), is_admin)
async def admin_add_stars_finish(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            await message.answer("❌ Введи положительное целое число.")
            return
    except ValueError:
        await message.answer("❌ Введи целое число.")
        return

    # Создаём инвойс на пополнение Stars
    await bot.send_invoice(
        chat_id=message.from_user.id,
        title="Пополнение Stars для бота",
        description=f"Пополнение баланса звёзд на {amount} XTR",
        payload="admin_stars_topup",          # специальный payload
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} Stars", amount=amount)],
        start_parameter="admin_stars_topup"
    )
    await message.answer(f"⭐ Отправлен счёт на {amount} Stars. Оплати его, чтобы пополнить баланс.")
    await state.clear()

@dp.message(StateFilter(AdminAction.sending_gift), is_admin)
async def admin_send_gift_finish(message: Message, state: FSMContext):
    # Парсим строку: gift_id.recipient_id.text
    try:
        parts = message.text.strip().split('.', 2)  # разделяем по точкам, максимум 2 разделения
        if len(parts) != 3:
            raise ValueError("Неверный формат. Ожидается: АЙДИ_ПОДАРКА.АЙДИ_ПОЛУЧАТЕЛЯ.ТЕКСТ")

        gift_id = parts[0].strip()
        recipient_id = int(parts[1].strip())
        text = parts[2].strip()

        if not gift_id or not text:
            raise ValueError("ID подарка и текст не могут быть пустыми.")

        # Отправляем подарок
        await bot.send_gift(
            chat_id=recipient_id,
            gift_id=gift_id,
            text=text
        )

        # Успех
        await message.answer(
            f"✅ Подарок успешно отправлен!\n"
            f"🆔 ID подарка: {gift_id}\n"
            f"👤 ID получателя: {recipient_id}\n"
            f"📝 Текст: {text}",
            reply_markup=admin_panel_keyboard()
        )
        await state.clear()

    except ValueError as e:
        await message.answer(
            f"❌ Ошибка формата: {e}\n"
            "Попробуй снова или нажми «Отмена».",
            reply_markup=InlineKeyboardBuilder().row(InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")).as_markup()
        )
        # остаёмся в том же состоянии
    except Exception as e:
        logger.error(f"Ошибка отправки подарка: {e}")
        await message.answer(
            f"❌ Ошибка при отправке подарка:\n{e}\n"
            "Вернулся в админ-панель.",
            reply_markup=admin_panel_keyboard()
        )
        await state.clear()

# ---------- Вспомогательная функция для форматирования списка подарков ----------
def format_gift_list(gifts: List) -> str:
    lines = []
    for gift in gifts:
        # gift может содержать поля: id, sticker (с эмодзи), star_count, etc.
        # Используем sticker_emoji, если есть, иначе "⭐"
        emoji = getattr(gift, 'sticker_emoji', None) or "⭐"
        gift_id = getattr(gift, 'id', '?')
        price = getattr(gift, 'star_count', '?')
        lines.append(f"{emoji} ID: {gift_id} — {price} ⭐")
    return "\n".join(lines) if lines else "Нет доступных подарков."

# ---------- Ответ администраторам (существующая команда) ----------
@dp.message(Command("reply"), is_admin)
async def admin_reply(message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Использование: /reply <user_id> <текст>")
        return
    try:
        target_id = int(args[1])
        reply_text = " ".join(args[2:])
        await bot.send_message(chat_id=target_id, text=f"👨‍💻 Ответ поддержки:\n{reply_text}")
        await message.answer("✅ Ответ отправлен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

@dp.message()
async def fallback(message: Message):
    await message.answer(
        "🤔 Неизвестная команда. Воспользуйся кнопками меню или введи /help.",
        reply_markup=main_menu_keyboard()
    )

# ---------- Запуск ----------
async def main():
    # База данных создаётся автоматически в load_db() при первом обращении
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
