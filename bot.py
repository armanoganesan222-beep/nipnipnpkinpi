import asyncio
import sqlite3
import time

import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)

import config


# =========================================================
# BOT
# =========================================================

bot = Bot(
    token=config.BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()

http_session = None


# =========================================================
# DATABASE
# =========================================================

db = sqlite3.connect(
    config.DATABASE,
    check_same_thread=False
)

db.row_factory = sqlite3.Row


def init_db():

    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            state TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            direction TEXT NOT NULL,
            asset TEXT NOT NULL,
            crypto_amount REAL NOT NULL,
            market_rate REAL NOT NULL,
            exchange_rate REAL NOT NULL,
            rub_amount REAL NOT NULL,
            payout_details TEXT,
            receipt_file_id TEXT,
            tx_hash TEXT,
            status TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)

    defaults = {
        "payment_details": config.PAYMENT_DETAILS,
        "ton_wallet": config.TON_RECEIVE_WALLET,
        "usdt_wallet": config.USDT_RECEIVE_WALLET,
        "support": config.SUPPORT_USERNAME,

        "ton_buy_markup": config.TON_BUY_MARKUP,
        "usdt_buy_markup": config.USDT_BUY_MARKUP,

        "ton_sell_discount": config.TON_SELL_DISCOUNT,
        "usdt_sell_discount": config.USDT_SELL_DISCOUNT,

        "min_ton_buy": config.MIN_TON_BUY,
        "min_usdt_buy": config.MIN_USDT_BUY,

        "min_ton_sell": config.MIN_TON_SELL,
        "min_usdt_sell": config.MIN_USDT_SELL
    }

    for key, value in defaults.items():

        db.execute("""
            INSERT OR IGNORE INTO settings
            (key, value)
            VALUES (?, ?)
        """, (key, str(value)))

    db.commit()


def get_setting(key):

    row = db.execute("""
        SELECT value
        FROM settings
        WHERE key = ?
    """, (key,)).fetchone()

    return row["value"] if row else ""


def set_setting(key, value):

    db.execute("""
        INSERT INTO settings
        (key, value)
        VALUES (?, ?)

        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (key, str(value)))

    db.commit()


# =========================================================
# USER STATE
# =========================================================

def set_state(user_id, state):

    db.execute("""
        INSERT INTO users
        (user_id, state)
        VALUES (?, ?)

        ON CONFLICT(user_id)
        DO UPDATE SET state = excluded.state
    """, (user_id, state))

    db.commit()


def get_state(user_id):

    row = db.execute("""
        SELECT state
        FROM users
        WHERE user_id = ?
    """, (user_id,)).fetchone()

    return row["state"] if row else None


def clear_state(user_id):

    db.execute("""
        UPDATE users
        SET state = NULL
        WHERE user_id = ?
    """, (user_id,))

    db.commit()


# =========================================================
# ORDERS
# =========================================================

def create_order(
    user_id,
    username,
    direction,
    asset,
    crypto_amount,
    market_rate,
    exchange_rate,
    rub_amount
):

    cur = db.execute("""
        INSERT INTO orders (
            user_id,
            username,
            direction,
            asset,
            crypto_amount,
            market_rate,
            exchange_rate,
            rub_amount,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        user_id,
        username,
        direction,
        asset,
        crypto_amount,
        market_rate,
        exchange_rate,
        rub_amount,
        "created",
        int(time.time())
    ))

    db.commit()

    return cur.lastrowid


def get_order(order_id):

    return db.execute("""
        SELECT *
        FROM orders
        WHERE id = ?
    """, (order_id,)).fetchone()


def update_order(order_id, **fields):

    if not fields:
        return

    query = ", ".join(
        f"{key} = ?"
        for key in fields
    )

    values = list(fields.values())
    values.append(order_id)

    db.execute(
        f"""
        UPDATE orders
        SET {query}
        WHERE id = ?
        """,
        values
    )

    db.commit()


def get_pending_orders():

    return db.execute("""
        SELECT *
        FROM orders
        WHERE status IN (
            'waiting_payment',
            'waiting_crypto'
        )
        ORDER BY id DESC
    """).fetchall()


# =========================================================
# RATE
# =========================================================

rate_cache = {
    "ton": None,
    "usdt": None,
    "time": 0
}


async def get_rates():

    now = time.time()

    if (
        rate_cache["ton"] is not None
        and rate_cache["usdt"] is not None
        and now - rate_cache["time"]
        < config.RATE_CACHE_SECONDS
    ):
        return (
            rate_cache["ton"],
            rate_cache["usdt"]
        )

    params = {
        "ids": "the-open-network,tether",
        "vs_currencies": "rub"
    }

    try:

        async with http_session.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params=params,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as response:

            if response.status != 200:
                raise RuntimeError(
                    f"CoinGecko HTTP {response.status}"
                )

            data = await response.json()

            ton = float(
                data["the-open-network"]["rub"]
            )

            usdt = float(
                data["tether"]["rub"]
            )

            rate_cache["ton"] = ton
            rate_cache["usdt"] = usdt
            rate_cache["time"] = now

            return ton, usdt

    except Exception:

        if (
            rate_cache["ton"] is not None
            and rate_cache["usdt"] is not None
        ):
            return (
                rate_cache["ton"],
                rate_cache["usdt"]
            )

        raise


def calculate_rate(
    asset,
    market_rate,
    direction
):

    if asset == "TON":

        if direction == "buy":
            return market_rate * (
                1 + float(
                    get_setting("ton_buy_markup")
                ) / 100
            )

        return market_rate * (
            1 - float(
                get_setting("ton_sell_discount")
            ) / 100
        )

    if asset == "USDT":

        if direction == "buy":
            return market_rate * (
                1 + float(
                    get_setting("usdt_buy_markup")
                ) / 100
            )

        return market_rate * (
            1 - float(
                get_setting("usdt_sell_discount")
            ) / 100
        )


# =========================================================
# HELPERS
# =========================================================

def fmt(value):

    return (
        f"{value:,.8f}"
        .replace(",", " ")
        .rstrip("0")
        .rstrip(".")
    )


def parse_amount(text):

    try:

        value = float(
            text
            .replace(",", ".")
            .replace(" ", "")
            .strip()
        )

        if value <= 0:
            return None

        return value

    except ValueError:
        return None


def main_keyboard(user_id=None):

    buttons = [
        [
            InlineKeyboardButton(
                text="🟢 Купить TON",
                callback_data="buy_ton"
            ),
            InlineKeyboardButton(
                text="🟢 Купить USDT",
                callback_data="buy_usdt"
            )
        ],
        [
            InlineKeyboardButton(
                text="🔴 Продать TON",
                callback_data="sell_ton"
            ),
            InlineKeyboardButton(
                text="🔴 Продать USDT",
                callback_data="sell_usdt"
            )
        ],
        [
            InlineKeyboardButton(
                text="💱 Курсы",
                callback_data="rates"
            ),
            InlineKeyboardButton(
                text="🆘 Поддержка",
                callback_data="support"
            )
        ]
    ]

    if user_id == config.ADMIN_ID:

        buttons.append([
            InlineKeyboardButton(
                text="🛠 Админ-панель",
                callback_data="admin"
            )
        ])

    return InlineKeyboardMarkup(
        inline_keyboard=buttons
    )


def back_button():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="back"
                )
            ]
        ]
    )


def admin_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📥 Заявки",
                    callback_data="a_orders"
                ),
                InlineKeyboardButton(
                    text="📊 Статистика",
                    callback_data="a_stats"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💱 Курсы",
                    callback_data="a_rates"
                ),
                InlineKeyboardButton(
                    text="⚙️ Настройки",
                    callback_data="a_settings"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔄 Обновить курс",
                    callback_data="a_refresh"
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Главное меню",
                    callback_data="back"
                )
            ]
        ]
    )


def settings_keyboard():

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏦 Реквизиты",
                    callback_data="set_payment"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💎 TON-адрес",
                    callback_data="set_ton_wallet"
                ),
                InlineKeyboardButton(
                    text="💵 USDT-адрес",
                    callback_data="set_usdt_wallet"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🆘 Поддержка",
                    callback_data="set_support"
                )
            ],
            [
                InlineKeyboardButton(
                    text="📈 Наценки",
                    callback_data="set_markup"
                ),
                InlineKeyboardButton(
                    text="📉 Скидки",
                    callback_data="set_discount"
                )
            ],
            [
                InlineKeyboardButton(
                    text="💰 Минимумы",
                    callback_data="set_limits"
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Админ-панель",
                    callback_data="admin"
                )
            ]
        ]
    )


def admin_order_keyboard(order_id):

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{order_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{order_id}"
                )
            ]
        ]
    )


# =========================================================
# START
# =========================================================

@dp.message(Command("start"))
async def start(message: Message):

    clear_state(message.from_user.id)

    await message.answer(
        "💱 <b>CRYPTO EXCHANGE</b>\n\n"
        "Покупка и продажа криптовалюты за ₽.\n\n"
        "💎 TON\n"
        "💵 USDT\n\n"
        "📈 Автоматический курс\n"
        "⚡ Быстрая обработка\n"
        "🔐 Ручная проверка\n\n"
        "Выберите действие:",
        reply_markup=main_keyboard(
            message.from_user.id
        )
    )


# =========================================================
# BACK
# =========================================================

@dp.callback_query(F.data == "back")
async def back(callback: CallbackQuery):

    clear_state(callback.from_user.id)

    await callback.message.edit_text(
        "💱 <b>CRYPTO EXCHANGE</b>\n\n"
        "Выберите действие:",
        reply_markup=main_keyboard(
            callback.from_user.id
        )
    )

    await callback.answer()


# =========================================================
# RATES
# =========================================================

@dp.callback_query(F.data == "rates")
async def rates(callback: CallbackQuery):

    try:

        ton, usdt = await get_rates()

        ton_buy = calculate_rate(
            "TON", ton, "buy"
        )

        ton_sell = calculate_rate(
            "TON", ton, "sell"
        )

        usdt_buy = calculate_rate(
            "USDT", usdt, "buy"
        )

        usdt_sell = calculate_rate(
            "USDT", usdt, "sell"
        )

        text = (
            "💱 <b>КУРСЫ</b>\n\n"

            "💎 <b>TON</b>\n"
            f"🟢 Покупка: <b>{ton_buy:,.2f} ₽</b>\n"
            f"🔴 Продажа: <b>{ton_sell:,.2f} ₽</b>\n\n"

            "💵 <b>USDT</b>\n"
            f"🟢 Покупка: <b>{usdt_buy:,.2f} ₽</b>\n"
            f"🔴 Продажа: <b>{usdt_sell:,.2f} ₽</b>\n\n"

            "🔄 Курс обновляется автоматически."
        )

    except Exception:

        text = (
            "⚠️ <b>Курс временно недоступен.</b>\n\n"
            "Попробуйте немного позже."
        )

    await callback.message.edit_text(
        text,
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# SUPPORT
# =========================================================

@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):

    await callback.message.edit_text(
        "🆘 <b>ПОДДЕРЖКА</b>\n\n"
        f"Связь: {get_setting('support')}",
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# BUY
# =========================================================

@dp.callback_query(
    F.data.in_({"buy_ton", "buy_usdt"})
)
async def buy_start(callback: CallbackQuery):

    asset = (
        "TON"
        if callback.data == "buy_ton"
        else "USDT"
    )

    minimum = float(
        get_setting(
            "min_ton_buy"
            if asset == "TON"
            else "min_usdt_buy"
        )
    )

    set_state(
        callback.from_user.id,
        f"buy_amount:{asset}"
    )

    await callback.message.edit_text(
        f"🟢 <b>ПОКУПКА {asset}</b>\n\n"
        f"Минимум: <b>{fmt(minimum)} {asset}</b>\n\n"
        f"Введите количество {asset}.\n\n"
        "Например: <code>10</code>",
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# SELL
# =========================================================

@dp.callback_query(
    F.data.in_({"sell_ton", "sell_usdt"})
)
async def sell_start(callback: CallbackQuery):

    asset = (
        "TON"
        if callback.data == "sell_ton"
        else "USDT"
    )

    minimum = float(
        get_setting(
            "min_ton_sell"
            if asset == "TON"
            else "min_usdt_sell"
        )
    )

    set_state(
        callback.from_user.id,
        f"sell_amount:{asset}"
    )

    await callback.message.edit_text(
        f"🔴 <b>ПРОДАЖА {asset}</b>\n\n"
        f"Минимум: <b>{fmt(minimum)} {asset}</b>\n\n"
        f"Введите количество {asset}.\n\n"
        "Например: <code>10</code>",
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# TEXT INPUT
# =========================================================

@dp.message(F.text)
async def text_handler(message: Message):

    user_id = message.from_user.id
    state = get_state(user_id)

    if not state:
        return


    # -----------------------------------------------------
    # BUY AMOUNT
    # -----------------------------------------------------

    if state.startswith("buy_amount:"):

        asset = state.split(":")[1]
        amount = parse_amount(message.text)

        if amount is None:

            await message.answer(
                "❌ Введите корректное число."
            )
            return

        minimum = float(
            get_setting(
                "min_ton_buy"
                if asset == "TON"
                else "min_usdt_buy"
            )
        )

        if amount < minimum:

            await message.answer(
                f"❌ Минимум: "
                f"<b>{fmt(minimum)} {asset}</b>"
            )
            return

        try:

            ton, usdt = await get_rates()

            market = ton if asset == "TON" else usdt

            rate = calculate_rate(
                asset,
                market,
                "buy"
            )

            rub = amount * rate

            order_id = create_order(
                user_id,
                message.from_user.username,
                "buy",
                asset,
                amount,
                market,
                rate,
                rub
            )

            set_state(
                user_id,
                f"buy_payment:{order_id}"
            )

            await message.answer(
                f"🟢 <b>ПОКУПКА {asset}</b>\n\n"
                f"💎 Количество: "
                f"<b>{fmt(amount)} {asset}</b>\n"
                f"💱 Курс: <b>{rate:,.2f} ₽</b>\n\n"
                f"💰 К оплате:\n"
                f"<b>{rub:,.2f} ₽</b>\n\n"
                f"{get_setting('payment_details')}\n\n"
                "После оплаты отправьте чек.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="❌ Отменить",
                                callback_data="back"
                            )
                        ]
                    ]
                )
            )

        except Exception:

            await message.answer(
                "⚠️ Не удалось получить курс."
            )

        return


    # -----------------------------------------------------
    # SELL AMOUNT
    # -----------------------------------------------------

    if state.startswith("sell_amount:"):

        asset = state.split(":")[1]
        amount = parse_amount(message.text)

        if amount is None:

            await message.answer(
                "❌ Введите корректное число."
            )
            return

        minimum = float(
            get_setting(
                "min_ton_sell"
                if asset == "TON"
                else "min_usdt_sell"
            )
        )

        if amount < minimum:

            await message.answer(
                f"❌ Минимум: "
                f"<b>{fmt(minimum)} {asset}</b>"
            )
            return

        try:

            ton, usdt = await get_rates()

            market = ton if asset == "TON" else usdt

            rate = calculate_rate(
                asset,
                market,
                "sell"
            )

            rub = amount * rate

            order_id = create_order(
                user_id,
                message.from_user.username,
                "sell",
                asset,
                amount,
                market,
                rate,
                rub
            )

            set_state(
                user_id,
                f"sell_payout:{order_id}"
            )

            await message.answer(
                f"🔴 <b>ПРОДАЖА {asset}</b>\n\n"
                f"💎 Вы отдаёте: "
                f"<b>{fmt(amount)} {asset}</b>\n"
                f"💱 Курс выкупа: "
                f"<b>{rate:,.2f} ₽</b>\n\n"
                f"💰 Получите:\n"
                f"<b>{rub:,.2f} ₽</b>\n\n"
                "🏦 Теперь отправьте реквизиты,\n"
                "куда перевести рубли."
            )

        except Exception:

            await message.answer(
                "⚠️ Не удалось получить курс."
            )

        return


    # -----------------------------------------------------
    # PAYOUT DETAILS
    # -----------------------------------------------------

    if state.startswith("sell_payout:"):

        order_id = int(
            state.split(":")[1]
        )

        update_order(
            order_id,
            payout_details=message.text
        )

        order = get_order(order_id)

        wallet = (
            get_setting("ton_wallet")
            if order["asset"] == "TON"
            else get_setting("usdt_wallet")
        )

        set_state(
            user_id,
            f"sell_crypto:{order_id}"
        )

        await message.answer(
            "📥 <b>РЕКВИЗИТЫ ПОЛУЧЕНЫ</b>\n\n"
            f"Отправьте:\n"
            f"<b>{fmt(order['crypto_amount'])} "
            f"{order['asset']}</b>\n\n"
            "📍 <b>АДРЕС:</b>\n"
            f"<code>{wallet}</code>\n\n"
            "⚠️ USDT принимается в сети TON.\n\n"
            "После перевода отправьте TX hash."
        )

        return


    # -----------------------------------------------------
    # TX HASH
    # -----------------------------------------------------

    if state.startswith("sell_crypto:"):

        order_id = int(
            state.split(":")[1]
        )

        update_order(
            order_id,
            tx_hash=message.text,
            status="waiting_crypto"
        )

        clear_state(user_id)

        await message.answer(
            "📨 <b>ЗАЯВКА ОТПРАВЛЕНА</b>\n\n"
            "Администратор проверит перевод "
            "и обработает заявку.",
            reply_markup=main_keyboard(user_id)
        )

        await send_order_to_admin(order_id)

        return


# =========================================================
# PHOTO
# =========================================================

@dp.message(F.photo)
async def photo_handler(message: Message):

    user_id = message.from_user.id
    state = get_state(user_id)

    if not state:
        return

    if state.startswith("buy_payment:"):

        order_id = int(
            state.split(":")[1]
        )

        update_order(
            order_id,
            receipt_file_id=message.photo[-1].file_id,
            status="waiting_payment"
        )

        clear_state(user_id)

        await message.answer(
            "📨 <b>ЧЕК ПОЛУЧЕН</b>\n\n"
            "Заявка отправлена администратору.",
            reply_markup=main_keyboard(user_id)
        )

        await send_order_to_admin(order_id)


# =========================================================
# ADMIN ORDER
# =========================================================

async def send_order_to_admin(order_id):

    order = get_order(order_id)

    if not order:
        return

    direction = (
        "🟢 ПОКУПКА"
        if order["direction"] == "buy"
        else "🔴 ПРОДАЖА"
    )

    username = (
        f"@{order['username']}"
        if order["username"]
        else "нет username"
    )

    text = (
        f"📥 <b>ЗАЯВКА #{order['id']}</b>\n\n"
        f"👤 {username}\n"
        f"🆔 <code>{order['user_id']}</code>\n\n"
        f"📌 {direction}\n"
        f"💎 {order['asset']}\n"
        f"📦 <b>{fmt(order['crypto_amount'])}</b>\n\n"
        f"📊 Рынок: "
        f"<b>{order['market_rate']:,.2f} ₽</b>\n"
        f"💱 Курс обменника: "
        f"<b>{order['exchange_rate']:,.2f} ₽</b>\n"
        f"💰 Сумма: "
        f"<b>{order['rub_amount']:,.2f} ₽</b>\n"
    )

    if order["payout_details"]:

        text += (
            "\n🏦 <b>Реквизиты выплаты:</b>\n"
            f"<code>{order['payout_details']}</code>\n"
        )

    if order["tx_hash"]:

        text += (
            "\n🔗 <b>TX:</b>\n"
            f"<code>{order['tx_hash']}</code>\n"
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Одобрить",
                    callback_data=f"approve:{order_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отклонить",
                    callback_data=f"reject:{order_id}"
                )
            ]
        ]
    )

    if order["receipt_file_id"]:

        await bot.send_photo(
            config.ADMIN_ID,
            order["receipt_file_id"],
            caption=text,
            reply_markup=keyboard
        )

    else:

        await bot.send_message(
            config.ADMIN_ID,
            text,
            reply_markup=keyboard
        )


# =========================================================
# ADMIN MAIN
# =========================================================

@dp.callback_query(F.data == "admin")
async def admin(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        await callback.answer(
            "⛔ Доступ запрещён.",
            show_alert=True
        )
        return

    await callback.message.edit_text(
        "🛠 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
        "Управление обменником:",
        reply_markup=admin_keyboard()
    )

    await callback.answer()


# =========================================================
# ADMIN ORDERS
# =========================================================

@dp.callback_query(F.data == "a_orders")
async def admin_orders(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    orders = get_pending_orders()

    if not orders:

        await callback.message.edit_text(
            "📭 <b>ЗАЯВОК НЕТ</b>\n\n"
            "Все заявки обработаны.",
            reply_markup=admin_keyboard()
        )

        await callback.answer()
        return

    await callback.message.edit_text(
        f"📥 <b>ЗАЯВКИ: {len(orders)}</b>\n\n"
        "Все ожидающие заявки отправлены ниже.",
        reply_markup=admin_keyboard()
    )

    for order in orders:
        await send_order_to_admin(order["id"])

    await callback.answer("Готово")


# =========================================================
# ADMIN RATES
# =========================================================

@dp.callback_query(F.data == "a_rates")
async def admin_rates(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    try:

        ton, usdt = await get_rates()

        text = (
            "💱 <b>КУРСЫ</b>\n\n"

            f"💎 TON\n"
            f"Рынок: <b>{ton:,.2f} ₽</b>\n"
            f"Покупка: <b>{calculate_rate('TON', ton, 'buy'):,.2f} ₽</b>\n"
            f"Продажа: <b>{calculate_rate('TON', ton, 'sell'):,.2f} ₽</b>\n\n"

            f"💵 USDT\n"
            f"Рынок: <b>{usdt:,.2f} ₽</b>\n"
            f"Покупка: <b>{calculate_rate('USDT', usdt, 'buy'):,.2f} ₽</b>\n"
            f"Продажа: <b>{calculate_rate('USDT', usdt, 'sell'):,.2f} ₽</b>"
        )

    except Exception:

        text = "⚠️ Не удалось получить курс."

    await callback.message.edit_text(
        text,
        reply_markup=admin_keyboard()
    )

    await callback.answer()


# =========================================================
# ADMIN REFRESH
# =========================================================

@dp.callback_query(F.data == "a_refresh")
async def admin_refresh(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    rate_cache["time"] = 0

    try:

        ton, usdt = await get_rates()

        await callback.answer(
            f"TON {ton:,.2f} ₽ | USDT {usdt:,.2f} ₽"
        )

    except Exception:

        await callback.answer(
            "Не удалось обновить курс.",
            show_alert=True
        )


# =========================================================
# ADMIN STATS
# =========================================================

@dp.callback_query(F.data == "a_stats")
async def admin_stats(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    users = db.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    total = db.execute(
        "SELECT COUNT(*) FROM orders"
    ).fetchone()[0]

    pending = db.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status IN (
            'waiting_payment',
            'waiting_crypto'
        )
    """).fetchone()[0]

    completed = db.execute("""
        SELECT COUNT(*)
        FROM orders
        WHERE status = 'approved'
    """).fetchone()[0]

    await callback.message.edit_text(
        "📊 <b>СТАТИСТИКА</b>\n\n"
        f"👥 Пользователей: <b>{users}</b>\n"
        f"📋 Заявок: <b>{total}</b>\n"
        f"⏳ Ожидают: <b>{pending}</b>\n"
        f"✅ Завершено: <b>{completed}</b>",
        reply_markup=admin_keyboard()
    )

    await callback.answer()


# =========================================================
# SETTINGS MENU
# =========================================================

@dp.callback_query(F.data == "a_settings")
async def settings(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    await callback.message.edit_text(
        "⚙️ <b>НАСТРОЙКИ ОБМЕННИКА</b>\n\n"
        "Здесь можно изменить всё без "
        "перезапуска бота.",
        reply_markup=settings_keyboard()
    )

    await callback.answer()


# =========================================================
# SETTINGS HELP
# =========================================================

async def setting_input(
    callback,
    state,
    title,
    current
):

    set_state(
        callback.from_user.id,
        state
    )

    await callback.message.edit_text(
        f"⚙️ <b>{title}</b>\n\n"
        f"Текущее значение:\n"
        f"<code>{current}</code>\n\n"
        "Отправьте новое значение.",
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# PAYMENT
# =========================================================

@dp.callback_query(F.data == "set_payment")
async def set_payment(callback: CallbackQuery):

    await setting_input(
        callback,
        "setting:payment",
        "РЕКВИЗИТЫ",
        get_setting("payment_details")
    )


# =========================================================
# TON WALLET
# =========================================================

@dp.callback_query(F.data == "set_ton_wallet")
async def set_ton_wallet(callback: CallbackQuery):

    await setting_input(
        callback,
        "setting:ton_wallet",
        "TON АДРЕС",
        get_setting("ton_wallet")
    )


# =========================================================
# USDT WALLET
# =========================================================

@dp.callback_query(F.data == "set_usdt_wallet")
async def set_usdt_wallet(callback: CallbackQuery):

    await setting_input(
        callback,
        "setting:usdt_wallet",
        "USDT АДРЕС",
        get_setting("usdt_wallet")
    )


# =========================================================
# SUPPORT
# =========================================================

@dp.callback_query(F.data == "set_support")
async def set_support(callback: CallbackQuery):

    await setting_input(
        callback,
        "setting:support",
        "ПОДДЕРЖКА",
        get_setting("support")
    )


# =========================================================
# MARKUP
# =========================================================

@dp.callback_query(F.data == "set_markup")
async def set_markup(callback: CallbackQuery):

    set_state(
        callback.from_user.id,
        "setting:markup"
    )

    await callback.message.edit_text(
        "📈 <b>НАЦЕНКИ ПРИ ПОКУПКЕ</b>\n\n"

        f"TON: <b>{get_setting('ton_buy_markup')}%</b>\n"
        f"USDT: <b>{get_setting('usdt_buy_markup')}%</b>\n\n"

        "Отправьте в формате:\n"
        "<code>5 3</code>\n\n"
        "Первое число — TON\n"
        "Второе — USDT",
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# DISCOUNT
# =========================================================

@dp.callback_query(F.data == "set_discount")
async def set_discount(callback: CallbackQuery):

    set_state(
        callback.from_user.id,
        "setting:discount"
    )

    await callback.message.edit_text(
        "📉 <b>СКИДКИ ПРИ ПРОДАЖЕ</b>\n\n"

        f"TON: <b>{get_setting('ton_sell_discount')}%</b>\n"
        f"USDT: <b>{get_setting('usdt_sell_discount')}%</b>\n\n"

        "Отправьте:\n"
        "<code>3 2</code>\n\n"
        "Первое число — TON\n"
        "Второе — USDT",
        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# LIMITS
# =========================================================

@dp.callback_query(F.data == "set_limits")
async def set_limits(callback: CallbackQuery):

    set_state(
        callback.from_user.id,
        "setting:limits"
    )

    await callback.message.edit_text(
        "💰 <b>МИНИМАЛЬНЫЕ СУММЫ</b>\n\n"

        f"Покупка TON: "
        f"<b>{get_setting('min_ton_buy')}</b>\n"

        f"Покупка USDT: "
        f"<b>{get_setting('min_usdt_buy')}</b>\n\n"

        f"Продажа TON: "
        f"<b>{get_setting('min_ton_sell')}</b>\n"

        f"Продажа USDT: "
        f"<b>{get_setting('min_usdt_sell')}</b>\n\n"

        "Отправьте 4 числа:\n"
        "<code>0.1 10 0.1 10</code>\n\n"

        "Порядок:\n"
        "TON BUY / USDT BUY / TON SELL / USDT SELL",

        reply_markup=back_button()
    )

    await callback.answer()


# =========================================================
# SETTINGS TEXT
# =========================================================

@dp.message(F.text)
async def settings_text(message: Message):

    user_id = message.from_user.id
    state = get_state(user_id)

    if not state:
        return

    # -----------------------------------------------------
    # PAYMENT
    # -----------------------------------------------------

    if state == "setting:payment":

        set_setting(
            "payment_details",
            message.text
        )

        clear_state(user_id)

        await message.answer(
            "✅ <b>Реквизиты изменены.</b>",
            reply_markup=admin_keyboard()
        )

        return


    # -----------------------------------------------------
    # TON WALLET
    # -----------------------------------------------------

    if state == "setting:ton_wallet":

        set_setting(
            "ton_wallet",
            message.text.strip()
        )

        clear_state(user_id)

        await message.answer(
            "✅ <b>TON-адрес изменён.</b>",
            reply_markup=admin_keyboard()
        )

        return


    # -----------------------------------------------------
    # USDT WALLET
    # -----------------------------------------------------

    if state == "setting:usdt_wallet":

        set_setting(
            "usdt_wallet",
            message.text.strip()
        )

        clear_state(user_id)

        await message.answer(
            "✅ <b>USDT-адрес изменён.</b>",
            reply_markup=admin_keyboard()
        )

        return


    # -----------------------------------------------------
    # SUPPORT
    # -----------------------------------------------------

    if state == "setting:support":

        set_setting(
            "support",
            message.text.strip()
        )

        clear_state(user_id)

        await message.answer(
            "✅ <b>Поддержка изменена.</b>",
            reply_markup=admin_keyboard()
        )

        return


    # -----------------------------------------------------
    # MARKUP
    # -----------------------------------------------------

    if state == "setting:markup":

        parts = message.text.replace(",", ".").split()

        if len(parts) != 2:

            await message.answer(
                "❌ Нужно 2 числа.\n"
                "Например: <code>5 3</code>"
            )

            return

        try:

            ton = float(parts[0])
            usdt = float(parts[1])

            if ton < 0 or usdt < 0:
                raise ValueError

            set_setting(
                "ton_buy_markup",
                ton
            )

            set_setting(
                "usdt_buy_markup",
                usdt
            )

            clear_state(user_id)

            await message.answer(
                "✅ <b>Наценки изменены.</b>\n\n"
                f"TON: {ton}%\n"
                f"USDT: {usdt}%",
                reply_markup=admin_keyboard()
            )

        except ValueError:

            await message.answer(
                "❌ Введите корректные числа."
            )

        return


    # -----------------------------------------------------
    # DISCOUNT
    # -----------------------------------------------------

    if state == "setting:discount":

        parts = message.text.replace(",", ".").split()

        if len(parts) != 2:

            await message.answer(
                "❌ Нужно 2 числа.\n"
                "Например: <code>3 2</code>"
            )

            return

        try:

            ton = float(parts[0])
            usdt = float(parts[1])

            if ton < 0 or ton >= 100:
                raise ValueError

            if usdt < 0 or usdt >= 100:
                raise ValueError

            set_setting(
                "ton_sell_discount",
                ton
            )

            set_setting(
                "usdt_sell_discount",
                usdt
            )

            clear_state(user_id)

            await message.answer(
                "✅ <b>Скидки изменены.</b>\n\n"
                f"TON: {ton}%\n"
                f"USDT: {usdt}%",
                reply_markup=admin_keyboard()
            )

        except ValueError:

            await message.answer(
                "❌ Введите корректные значения от 0 до 99."
            )

        return


    # -----------------------------------------------------
    # LIMITS
    # -----------------------------------------------------

    if state == "setting:limits":

        parts = message.text.replace(",", ".").split()

        if len(parts) != 4:

            await message.answer(
                "❌ Нужно 4 числа.\n\n"
                "<code>0.1 10 0.1 10</code>"
            )

            return

        try:

            values = [
                float(x)
                for x in parts
            ]

            if any(x <= 0 for x in values):
                raise ValueError

            keys = [
                "min_ton_buy",
                "min_usdt_buy",
                "min_ton_sell",
                "min_usdt_sell"
            ]

            for key, value in zip(
                keys,
                values
            ):
                set_setting(
                    key,
                    value
                )

            clear_state(user_id)

            await message.answer(
                "✅ <b>Минимальные суммы изменены.</b>",
                reply_markup=admin_keyboard()
            )

        except ValueError:

            await message.answer(
                "❌ Введите 4 корректных числа."
            )

        return


    # =====================================================
    # BUY PAYMENT / SELL TX
    # =====================================================

    if state.startswith("buy_payment:"):

        await message.answer(
            "📸 Для подтверждения оплаты "
            "отправьте <b>фотографию чека</b>."
        )

        return


    if state.startswith("sell_crypto:"):

        order_id = int(
            state.split(":")[1]
        )

        update_order(
            order_id,
            tx_hash=message.text,
            status="waiting_crypto"
        )

        clear_state(user_id)

        await message.answer(
            "📨 <b>TX hash получен.</b>\n\n"
            "Заявка отправлена администратору.",
            reply_markup=main_keyboard(user_id)
        )

        await send_order_to_admin(order_id)


# =========================================================
# BUY RECEIPT PHOTO
# =========================================================

@dp.message(F.photo)
async def receipt_photo(message: Message):

    user_id = message.from_user.id
    state = get_state(user_id)

    if not state:
        return

    if state.startswith("buy_payment:"):

        order_id = int(
            state.split(":")[1]
        )

        update_order(
            order_id,
            receipt_file_id=message.photo[-1].file_id,
            status="waiting_payment"
        )

        clear_state(user_id)

        await message.answer(
            "📨 <b>ЧЕК ПОЛУЧЕН</b>\n\n"
            "Заявка отправлена администратору.",
            reply_markup=main_keyboard(user_id)
        )

        await send_order_to_admin(order_id)


# =========================================================
# APPROVE
# =========================================================

@dp.callback_query(
    F.data.startswith("approve:")
)
async def approve(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    order_id = int(
        callback.data.split(":")[1]
    )

    order = get_order(order_id)

    if not order:
        await callback.answer(
            "Заявка не найдена.",
            show_alert=True
        )
        return

    update_order(
        order_id,
        status="approved"
    )

    if order["direction"] == "buy":

        text = (
            f"✅ <b>ЗАЯВКА #{order_id} ОДОБРЕНА</b>\n\n"
            f"💎 {fmt(order['crypto_amount'])} "
            f"{order['asset']}\n\n"
            "Оплата подтверждена.\n"
            "Администратор отправит криптовалюту."
        )

    else:

        text = (
            f"✅ <b>ЗАЯВКА #{order_id} ОДОБРЕНА</b>\n\n"
            f"💰 К выплате: "
            f"<b>{order['rub_amount']:,.2f} ₽</b>\n\n"
            "Администратор отправит рубли."
        )

    try:

        await bot.send_message(
            order["user_id"],
            text,
            reply_markup=main_keyboard(
                order["user_id"]
            )
        )

    except Exception:
        pass

    await callback.message.edit_reply_markup(
        reply_markup=None
    )

    await callback.answer(
        "✅ Одобрено"
    )


# =========================================================
# REJECT
# =========================================================

@dp.callback_query(
    F.data.startswith("reject:")
)
async def reject(callback: CallbackQuery):

    if callback.from_user.id != config.ADMIN_ID:
        return

    order_id = int(
        callback.data.split(":")[1]
    )

    order = get_order(order_id)

    if not order:
        await callback.answer(
            "Заявка не найдена.",
            show_alert=True
        )
        return

    update_order(
        order_id,
        status="rejected"
    )

    try:

        await bot.send_message(
            order["user_id"],
            f"❌ <b>ЗАЯВКА #{order_id} ОТКЛОНЕНА</b>\n\n"
            f"Если есть вопросы: "
            f"{get_setting('support')}",
            reply_markup=main_keyboard(
                order["user_id"]
            )
        )

    except Exception:
        pass

    await callback.message.edit_reply_markup(
        reply_markup=None
    )

    await callback.answer(
        "❌ Отклонено"
    )


# =========================================================
# MAIN
# =========================================================

async def main():

    global http_session

    init_db()

    http_session = aiohttp.ClientSession()

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    await dp.start_polling(bot)


asyncio.run(main())