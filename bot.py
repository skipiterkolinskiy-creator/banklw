import asyncio
import logging
import os
import random
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_chat_id: Optional[int]
    admin_user_ids: set[int]
    db_path: str
    bank_name: str
    currency: str
    start_balance: int
    transfer_tax_percent: int
    max_loan: int
    loan_fee_percent: int
    loan_days: int
    crypto_rate: int
    alt_new_account_days: int
    alt_transfer_threshold: int
    alt_sender_count: int
    bankruptcy_freeze_days: int
    tax_grace_days: int


def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def read_settings() -> Settings:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is empty. Fill .env before running the bot.")

    admin_chat_raw = os.getenv("ADMIN_CHAT_ID", "").strip()
    admin_ids_raw = os.getenv("ADMIN_USER_IDS", "").strip()

    return Settings(
        bot_token=token,
        admin_chat_id=int(admin_chat_raw) if admin_chat_raw else None,
        admin_user_ids={int(x.strip()) for x in admin_ids_raw.split(",") if x.strip()},
        db_path=os.getenv("DB_PATH", "z_bank.sqlite3"),
        bank_name=os.getenv("BANK_NAME", "Z-Банк"),
        currency=os.getenv("CURRENCY", "лаймов"),
        start_balance=env_int("START_BALANCE", 35000),
        transfer_tax_percent=env_int("TRANSFER_TAX_PERCENT", 0),
        max_loan=env_int("MAX_LOAN", 5_000_000),
        loan_fee_percent=env_int("LOAN_FEE_PERCENT", 10),
        loan_days=env_int("LOAN_DAYS", 7),
        crypto_rate=env_int("CRYPTO_RATE", 1000),
        alt_new_account_days=env_int("ALT_NEW_ACCOUNT_DAYS", 2),
        alt_transfer_threshold=env_int("ALT_TRANSFER_THRESHOLD", 100000),
        alt_sender_count=env_int("ALT_SENDER_COUNT", 3),
        bankruptcy_freeze_days=env_int("BANKRUPTCY_FREEZE_DAYS", 3),
        tax_grace_days=env_int("TAX_GRACE_DAYS", 7),
    )


settings = read_settings()
router = Router()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat(timespec="seconds")


def money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def unique_value(db: sqlite3.Connection, table: str, column: str, factory) -> str:
    while True:
        value = factory()
        exists = db.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (value,)).fetchone()
        if not exists:
            return value


def make_card_number() -> str:
    return "2200" + "".join(str(random.randint(0, 9)) for _ in range(12))


def make_z_id() -> str:
    return "Z" + "".join(str(random.randint(0, 9)) for _ in range(10))


def make_crypto_wallet() -> str:
    return "LW" + secrets.token_hex(10).upper()


def init_db() -> None:
    with closing(connect()) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT NOT NULL,
                balance INTEGER NOT NULL DEFAULT 0,
                savings INTEGER NOT NULL DEFAULT 0,
                crypto_balance INTEGER NOT NULL DEFAULT 0,
                loan INTEGER NOT NULL DEFAULT 0,
                loan_due_at TEXT,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                block_reason TEXT,
                block_until TEXT,
                is_verified INTEGER NOT NULL DEFAULT 0,
                is_mayor INTEGER NOT NULL DEFAULT 0,
                card_number TEXT UNIQUE,
                z_id TEXT UNIQUE,
                crypto_wallet TEXT UNIQUE,
                registered_at TEXT,
                last_tax_bill_at TEXT,
                tax_due_amount INTEGER NOT NULL DEFAULT 0,
                tax_due_at TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER,
                to_user_id INTEGER,
                amount INTEGER NOT NULL,
                type TEXT NOT NULL,
                source TEXT,
                comment TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS treasury (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                balance INTEGER NOT NULL DEFAULT 0,
                tax_amount INTEGER NOT NULL DEFAULT 2000,
                tax_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        migrations = {
            "savings": "ALTER TABLE users ADD COLUMN savings INTEGER NOT NULL DEFAULT 0",
            "crypto_balance": "ALTER TABLE users ADD COLUMN crypto_balance INTEGER NOT NULL DEFAULT 0",
            "loan_due_at": "ALTER TABLE users ADD COLUMN loan_due_at TEXT",
            "block_reason": "ALTER TABLE users ADD COLUMN block_reason TEXT",
            "block_until": "ALTER TABLE users ADD COLUMN block_until TEXT",
            "is_verified": "ALTER TABLE users ADD COLUMN is_verified INTEGER NOT NULL DEFAULT 0",
            "is_mayor": "ALTER TABLE users ADD COLUMN is_mayor INTEGER NOT NULL DEFAULT 0",
            "card_number": "ALTER TABLE users ADD COLUMN card_number TEXT",
            "z_id": "ALTER TABLE users ADD COLUMN z_id TEXT",
            "crypto_wallet": "ALTER TABLE users ADD COLUMN crypto_wallet TEXT",
            "registered_at": "ALTER TABLE users ADD COLUMN registered_at TEXT",
            "last_tax_bill_at": "ALTER TABLE users ADD COLUMN last_tax_bill_at TEXT",
            "tax_due_amount": "ALTER TABLE users ADD COLUMN tax_due_amount INTEGER NOT NULL DEFAULT 0",
            "tax_due_at": "ALTER TABLE users ADD COLUMN tax_due_at TEXT",
        }
        for column, sql in migrations.items():
            if column not in columns:
                db.execute(sql)

        tx_columns = {row["name"] for row in db.execute("PRAGMA table_info(transactions)").fetchall()}
        if "source" not in tx_columns:
            db.execute("ALTER TABLE transactions ADD COLUMN source TEXT")

        db.execute(
            "INSERT OR IGNORE INTO treasury (id, balance, tax_amount, tax_enabled, updated_at) VALUES (1, 0, 2000, 1, ?)",
            (now_iso(),),
        )
        db.commit()


def complete_identity(db: sqlite3.Connection, user_id: int) -> None:
    user = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user:
        return
    updates = {}
    if not user["card_number"]:
        updates["card_number"] = unique_value(db, "users", "card_number", make_card_number)
    if not user["z_id"]:
        updates["z_id"] = unique_value(db, "users", "z_id", make_z_id)
    if not user["crypto_wallet"]:
        updates["crypto_wallet"] = unique_value(db, "users", "crypto_wallet", make_crypto_wallet)
    if not user["registered_at"]:
        updates["registered_at"] = now_iso()
    if updates:
        parts = ", ".join(f"{key} = ?" for key in updates)
        db.execute(f"UPDATE users SET {parts} WHERE user_id = ?", (*updates.values(), user_id))


def remember_user(message: Message) -> sqlite3.Row:
    user = message.from_user
    if user is None:
        raise RuntimeError("Message has no Telegram user.")

    with closing(connect()) as db:
        existing = db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)).fetchone()
        if existing is None:
            card = unique_value(db, "users", "card_number", make_card_number)
            z_id = unique_value(db, "users", "z_id", make_z_id)
            wallet = unique_value(db, "users", "crypto_wallet", make_crypto_wallet)
            db.execute(
                """
                INSERT INTO users (
                    user_id, username, full_name, balance, card_number, z_id, crypto_wallet,
                    registered_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user.id, user.username, user.full_name, settings.start_balance, card, z_id, wallet, now_iso(), now_iso()),
            )
            add_transaction(db, "registration_bonus", settings.start_balance, to_user_id=user.id, comment="Первичная регистрация")
        else:
            db.execute(
                "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                (user.username, user.full_name, user.id),
            )
            complete_identity(db, user.id)
        unfreeze_expired_blocks(db, user.id)
        apply_overdue_loans(db, user.id)
        apply_overdue_taxes(db, user.id)
        db.commit()
        return db.execute("SELECT * FROM users WHERE user_id = ?", (user.id,)).fetchone()


def add_transaction(
    db: sqlite3.Connection,
    tx_type: str,
    amount: int,
    from_user_id: Optional[int] = None,
    to_user_id: Optional[int] = None,
    source: Optional[str] = None,
    comment: Optional[str] = None,
) -> None:
    db.execute(
        """
        INSERT INTO transactions (from_user_id, to_user_id, amount, type, source, comment, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (from_user_id, to_user_id, amount, tx_type, source, comment, now_iso()),
    )


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with closing(connect()) as db:
        return db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def find_user(token: str) -> Optional[sqlite3.Row]:
    clean = token.strip()
    with closing(connect()) as db:
        if clean.startswith("@"):
            return db.execute("SELECT * FROM users WHERE lower(username) = ?", (clean[1:].lower(),)).fetchone()
        upper = clean.upper()
        if upper.startswith("Z"):
            return db.execute("SELECT * FROM users WHERE upper(z_id) = ?", (upper,)).fetchone()
        if upper.startswith("LW"):
            return db.execute("SELECT * FROM users WHERE upper(crypto_wallet) = ?", (upper,)).fetchone()
        digits = "".join(ch for ch in clean if ch.isdigit())
        if len(digits) >= 4:
            return db.execute("SELECT * FROM users WHERE card_number = ? OR substr(card_number, -4) = ?", (digits, digits[-4:])).fetchone()
    return None


def resolve_target(message: Message, command: CommandObject) -> tuple[Optional[sqlite3.Row], list[str]]:
    args = (command.args or "").split()
    if message.reply_to_message and message.reply_to_message.from_user:
        return get_user(message.reply_to_message.from_user.id), args
    if not args:
        return None, []
    return find_user(args[0]), args[1:]


def parse_amount(raw: str) -> Optional[int]:
    try:
        amount = int(raw.replace(" ", "").replace("_", ""))
    except ValueError:
        return None
    return amount if amount > 0 else None


def is_admin_context(message: Message) -> bool:
    if settings.admin_chat_id is None or message.chat.id != settings.admin_chat_id:
        return False
    if not settings.admin_user_ids:
        return True
    return message.from_user is not None and message.from_user.id in settings.admin_user_ids


def is_admin_callback(callback: CallbackQuery) -> bool:
    message = callback.message
    if message is None or settings.admin_chat_id is None or message.chat.id != settings.admin_chat_id:
        return False
    if not settings.admin_user_ids:
        return True
    return callback.from_user.id in settings.admin_user_ids


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Статистика", callback_data="admin:stats"),
                InlineKeyboardButton(text="Топ", callback_data="admin:top"),
            ],
            [
                InlineKeyboardButton(text="Казна", callback_data="admin:treasury"),
                InlineKeyboardButton(text="Команды", callback_data="admin:help"),
            ],
        ]
    )


def account_status(user: sqlite3.Row) -> str:
    if user["is_blocked"]:
        if user["block_until"]:
            return f"заморожен до {parse_dt(user['block_until']).date()} ({user['block_reason'] or 'без причины'})"
        return f"заморожен ({user['block_reason'] or 'без причины'})"
    if user["is_verified"]:
        return "проверен"
    return "активен"


def history_lines(db: sqlite3.Connection, user_id: int, limit: int = 5) -> list[str]:
    rows = db.execute(
        """
        SELECT * FROM transactions
        WHERE from_user_id = ? OR to_user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (user_id, user_id, limit),
    ).fetchall()
    if not rows:
        return [f"{i}." for i in range(1, limit + 1)]

    lines = []
    for index, row in enumerate(rows, start=1):
        sign = "+" if row["to_user_id"] == user_id else "-"
        if row["type"] in {"repay", "tax_pay", "treasury_donate", "casino_loss", "bankruptcy_fee"}:
            sign = "-"
        comment = row["comment"] or row["type"]
        lines.append(f"{index}. {sign}{money(row['amount'])} {settings.currency} - {comment}")
    while len(lines) < limit:
        lines.append(f"{len(lines) + 1}.")
    return lines


async def send_900(bot: Bot, to_user_id: int, amount: int, sender: sqlite3.Row, source: str, new_balance: int) -> None:
    if source == "crypto":
        source_label = f"КриптоКошелек {sender['crypto_wallet'][:8]}"
        unit = "LWC"
    elif source == "zid":
        source_label = f"Z-ID ****{sender['z_id'][-4:]}"
        unit = settings.currency
    else:
        source_label = f"карта ****{sender['card_number'][-4:]}"
        unit = settings.currency
    try:
        await bot.send_message(
            to_user_id,
            "Новое уведомление!\n"
            "900:\n"
            f"Вы получили зачисление от {source_label}\n"
            f"Сумма: {money(amount)} {unit}\n"
            f"Ваш баланс: {money(new_balance)} {unit}",
        )
    except Exception as exc:
        logging.info("Cannot deliver 900 notification to %s: %s", to_user_id, exc)


def apply_overdue_loans(db: sqlite3.Connection, user_id: int) -> None:
    user = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    due_at = parse_dt(user["loan_due_at"]) if user and user["loan_due_at"] else None
    if user and user["loan"] > 0 and due_at and now_utc() >= due_at:
        debt = user["loan"]
        db.execute("UPDATE users SET balance = balance - ?, loan = 0, loan_due_at = NULL WHERE user_id = ?", (debt, user_id))
        add_transaction(db, "loan_overdue", debt, from_user_id=user_id, comment="Просрочка кредита: долг ушел в минус баланса")


def unfreeze_expired_blocks(db: sqlite3.Connection, user_id: int) -> None:
    user = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    block_until = parse_dt(user["block_until"]) if user and user["block_until"] else None
    if user and user["is_blocked"] and block_until and now_utc() >= block_until:
        db.execute("UPDATE users SET is_blocked = 0, block_reason = NULL, block_until = NULL WHERE user_id = ?", (user_id,))
        add_transaction(db, "auto_unfreeze", 0, to_user_id=user_id, comment="Авторазморозка после срока")


def apply_overdue_taxes(db: sqlite3.Connection, user_id: int) -> None:
    user = db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    due_at = parse_dt(user["tax_due_at"]) if user and user["tax_due_at"] else None
    if user and user["tax_due_amount"] > 0 and due_at and now_utc() >= due_at and not user["is_blocked"]:
        db.execute(
            "UPDATE users SET is_blocked = 1, block_reason = ?, block_until = NULL WHERE user_id = ?",
            ("неоплаченный налог", user_id),
        )
        add_transaction(db, "tax_freeze", 0, from_user_id=user_id, comment="Заморозка за неоплаченный налог")


def run_alt_guard(db: sqlite3.Connection, receiver_id: int) -> Optional[str]:
    cutoff = (now_utc() - timedelta(days=settings.alt_new_account_days)).isoformat(timespec="seconds")
    stats = db.execute(
        """
        SELECT COUNT(DISTINCT t.from_user_id) AS senders, COALESCE(SUM(t.amount), 0) AS total
        FROM transactions t
        JOIN users u ON u.user_id = t.from_user_id
        WHERE t.to_user_id = ?
          AND t.type IN ('transfer', 'z_transfer')
          AND u.created_at >= ?
          AND t.amount >= ?
        """,
        (receiver_id, cutoff, settings.start_balance // 2),
    ).fetchone()
    if stats["senders"] >= settings.alt_sender_count and stats["total"] >= settings.alt_transfer_threshold:
        reason = "подозрение на перелив стартовых денег с новых аккаунтов"
        db.execute("UPDATE users SET is_blocked = 1, block_reason = ? WHERE user_id = ?", (reason, receiver_id))
        add_transaction(db, "alt_guard_freeze", 0, to_user_id=receiver_id, comment=reason)
        return reason
    return None


async def transfer_money(message: Message, target: sqlite3.Row, amount: int, source: str, comment: str) -> None:
    sender = remember_user(message)
    if sender["is_blocked"]:
        await message.answer(f"Счет заморожен: {sender['block_reason'] or 'операции запрещены'}.")
        return
    if target["user_id"] == sender["user_id"]:
        await message.answer("Самому себе переводить нельзя.")
        return
    if target["is_blocked"]:
        await message.answer("Счет получателя заморожен.")
        return

    tax = amount * settings.transfer_tax_percent // 100
    total = amount + tax
    with closing(connect()) as db:
        fresh_sender = db.execute("SELECT * FROM users WHERE user_id = ?", (sender["user_id"],)).fetchone()
        fresh_target = db.execute("SELECT * FROM users WHERE user_id = ?", (target["user_id"],)).fetchone()
        if source == "crypto":
            if fresh_sender["crypto_balance"] < amount:
                await message.answer("Недостаточно средств на криптокошельке.")
                return
            db.execute("UPDATE users SET crypto_balance = crypto_balance - ? WHERE user_id = ?", (amount, sender["user_id"]))
            db.execute("UPDATE users SET crypto_balance = crypto_balance + ? WHERE user_id = ?", (amount, target["user_id"]))
            new_balance = fresh_target["crypto_balance"] + amount
        else:
            if fresh_sender["balance"] < total:
                await message.answer(f"Недостаточно средств. Нужно {money(total)} {settings.currency}.")
                return
            db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (total, sender["user_id"]))
            db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target["user_id"]))
            new_balance = fresh_target["balance"] + amount
            if tax:
                db.execute("UPDATE treasury SET balance = balance + ?, updated_at = ? WHERE id = 1", (tax, now_iso()))
                add_transaction(db, "transfer_tax", tax, from_user_id=sender["user_id"], source=source, comment="Комиссия в казну")
        add_transaction(db, "z_transfer" if source == "zid" else "transfer", amount, sender["user_id"], target["user_id"], source, comment)
        freeze_reason = run_alt_guard(db, target["user_id"])
        db.commit()

    await message.answer(f"Перевод выполнен: {money(amount)} {settings.currency}.")
    await send_900(message.bot, target["user_id"], amount, sender, source, new_balance)
    if freeze_reason and settings.admin_chat_id:
        await message.bot.send_message(settings.admin_chat_id, f"Анти-альт защита заморозила счет {target['full_name']}: {freeze_reason}.")


@router.message(Command("start"))
async def start(message: Message) -> None:
    account = remember_user(message)
    with closing(connect()) as db:
        lines = history_lines(db, account["user_id"], 5)
    await message.answer(
        "Привет! Я Олег! Твой виртуальный помощник!\n\n"
        f"Баланс: {money(account['balance'])} {settings.currency}\n"
        f"Z-ID: <code>{account['z_id']}</code>\n"
        f"Карта: ****{account['card_number'][-4:]}\n"
        f"Криптокошелек: <code>{account['crypto_wallet']}</code>\n\n"
        "История:\n" + "\n".join(lines) + "\n\n"
        "Ежедневный бонус нет."
    )


@router.message(Command("balance"))
async def balance(message: Message) -> None:
    account = remember_user(message)
    await message.answer(
        f"<b>{settings.bank_name}</b>\n"
        f"Клиент: {account['full_name']}\n"
        f"Статус: {account_status(account)}\n"
        f"Баланс карты ****{account['card_number'][-4:]}: <b>{money(account['balance'])} {settings.currency}</b>\n"
        f"Вклад: <b>{money(account['savings'])} {settings.currency}</b>\n"
        f"Криптокошелек {account['crypto_wallet'][:8]}: <b>{money(account['crypto_balance'])} LWC</b>\n"
        f"Кредит: <b>{money(account['loan'])} {settings.currency}</b>\n"
        f"Z-ID: <code>{account['z_id']}</code>"
    )


@router.message(Command("pay"))
async def pay(message: Message, command: CommandObject) -> None:
    target, args = resolve_target(message, command)
    if target is None or not args:
        await message.answer("Формат: /pay @user 100, /pay Z123 100 или ответом на сообщение /pay 100.")
        return
    amount = parse_amount(args[0])
    if amount is None:
        await message.answer("Сумма должна быть положительным числом.")
        return
    await transfer_money(message, target, amount, "card", "Перевод")


@router.message(Command("zpay"))
async def zpay(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Формат: /zpay Z1234567890 100.")
        return
    target = find_user(args[0])
    amount = parse_amount(args[1])
    if target is None or amount is None:
        await message.answer("Проверь Z-ID и сумму.")
        return
    await transfer_money(message, target, amount, "zid", "Анонимный перевод по Z-ID")


@router.message(Command("crypto"))
async def crypto(message: Message) -> None:
    account = remember_user(message)
    await message.answer(
        f"Криптокошелек: <code>{account['crypto_wallet']}</code>\n"
        f"Баланс: <b>{money(account['crypto_balance'])} LWC</b>\n"
        f"Курс: 1 LWC = {money(settings.crypto_rate)} {settings.currency}\n"
        "Команды: /buycrypto 10, /sellcrypto 10, /cryptopay LW... 10"
    )


@router.message(Command("buycrypto"))
async def buycrypto(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /buycrypto 10.")
        return
    cost = amount * settings.crypto_rate
    if account["balance"] < cost:
        await message.answer("Недостаточно средств на карте.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET balance = balance - ?, crypto_balance = crypto_balance + ? WHERE user_id = ?", (cost, amount, account["user_id"]))
        add_transaction(db, "buy_crypto", cost, from_user_id=account["user_id"], comment=f"Покупка {amount} LWC")
        db.commit()
    await message.answer(f"Куплено {money(amount)} LWC за {money(cost)} {settings.currency}.")


@router.message(Command("sellcrypto"))
async def sellcrypto(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /sellcrypto 10.")
        return
    if account["crypto_balance"] < amount:
        await message.answer("Недостаточно LWC.")
        return
    gain = amount * settings.crypto_rate
    with closing(connect()) as db:
        db.execute("UPDATE users SET crypto_balance = crypto_balance - ?, balance = balance + ? WHERE user_id = ?", (amount, gain, account["user_id"]))
        add_transaction(db, "sell_crypto", gain, to_user_id=account["user_id"], comment=f"Продажа {amount} LWC")
        db.commit()
    await message.answer(f"Продано {money(amount)} LWC. Зачислено {money(gain)} {settings.currency}.")


@router.message(Command("cryptopay"))
async def cryptopay(message: Message, command: CommandObject) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Формат: /cryptopay LW... 10.")
        return
    target = find_user(args[0])
    amount = parse_amount(args[1])
    if target is None or amount is None:
        await message.answer("Проверь кошелек и сумму.")
        return
    await transfer_money(message, target, amount, "crypto", "Криптоперевод")


@router.message(Command("deposit"))
async def deposit(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /deposit 1000.")
        return
    if account["is_blocked"] or account["balance"] < amount:
        await message.answer("Операция недоступна или недостаточно средств.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET balance = balance - ?, savings = savings + ? WHERE user_id = ?", (amount, amount, account["user_id"]))
        add_transaction(db, "deposit", amount, from_user_id=account["user_id"], comment="Пополнение вклада")
        db.commit()
    await message.answer(f"На вклад переведено {money(amount)} {settings.currency}.")


@router.message(Command("withdraw"))
async def withdraw(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /withdraw 1000.")
        return
    if account["is_blocked"] or account["savings"] < amount:
        await message.answer("Операция недоступна или недостаточно средств на вкладе.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET savings = savings - ?, balance = balance + ? WHERE user_id = ?", (amount, amount, account["user_id"]))
        add_transaction(db, "withdraw", amount, to_user_id=account["user_id"], comment="Снятие со вклада")
        db.commit()
    await message.answer(f"Со вклада снято {money(amount)} {settings.currency}.")


@router.message(Command("loan"))
async def loan(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None or amount > settings.max_loan:
        await message.answer(f"Можно взять кредит от 1 до {money(settings.max_loan)} {settings.currency}.")
        return
    if account["is_blocked"]:
        await message.answer("Счет заморожен.")
        return
    debt = amount + amount * settings.loan_fee_percent // 100
    due_at = now_utc() + timedelta(days=settings.loan_days)
    with closing(connect()) as db:
        db.execute(
            "UPDATE users SET balance = balance + ?, loan = loan + ?, loan_due_at = ? WHERE user_id = ?",
            (amount, debt, due_at.isoformat(timespec="seconds"), account["user_id"]),
        )
        add_transaction(db, "loan", amount, to_user_id=account["user_id"], comment=f"К возврату {money(debt)} до {due_at.date()}")
        db.commit()
    await message.answer(f"Кредит выдан: {money(amount)}. Если не погасить до {due_at.date()}, долг уйдет в минус баланса.")


@router.message(Command("repay"))
async def repay(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /repay 500.")
        return
    pay_amount = min(amount, account["loan"])
    if pay_amount <= 0:
        await message.answer("Активного кредита нет.")
        return
    if account["balance"] < pay_amount:
        await message.answer("Недостаточно средств. Можно просить помощь у админов или использовать /bankrupt.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET balance = balance - ?, loan = loan - ? WHERE user_id = ?", (pay_amount, pay_amount, account["user_id"]))
        db.execute("UPDATE users SET loan_due_at = NULL WHERE user_id = ? AND loan <= 0", (account["user_id"],))
        add_transaction(db, "repay", pay_amount, from_user_id=account["user_id"], comment="Погашение кредита")
        db.commit()
    await message.answer(f"Погашено {money(pay_amount)} {settings.currency}.")


@router.message(Command("bankrupt"))
async def bankrupt(message: Message) -> None:
    account = remember_user(message)
    if account["balance"] >= 0 and account["loan"] <= 0:
        await message.answer("Банкротство доступно только при долге или минусовом балансе.")
        return
    until = now_utc() + timedelta(days=settings.bankruptcy_freeze_days)
    with closing(connect()) as db:
        db.execute(
            """
            UPDATE users
            SET balance = 0, savings = 0, crypto_balance = 0, loan = 0, loan_due_at = NULL,
                is_blocked = 1, block_reason = ?, block_until = ?
            WHERE user_id = ?
            """,
            (f"банкротство до {until.date()}", until.isoformat(timespec="seconds"), account["user_id"]),
        )
        add_transaction(db, "bankruptcy", 0, from_user_id=account["user_id"], comment="Самобанкротство")
        db.commit()
    await message.answer(f"Банкротство оформлено. Долги списаны, счет заморожен на {settings.bankruptcy_freeze_days} дня.")


@router.message(Command("history"))
async def history(message: Message) -> None:
    account = remember_user(message)
    with closing(connect()) as db:
        lines = history_lines(db, account["user_id"], 10)
    await message.answer("<b>История операций</b>\n" + "\n".join(lines))


@router.message(Command("top"))
async def top(message: Message) -> None:
    remember_user(message)
    with closing(connect()) as db:
        rows = db.execute("SELECT full_name, balance FROM users WHERE is_blocked = 0 ORDER BY balance DESC LIMIT 10").fetchall()
    text = "<b>Топ клиентов Z-Банка</b>\n" + "\n".join(
        f"{index}. {row['full_name']} - {money(row['balance'])} {settings.currency}"
        for index, row in enumerate(rows, start=1)
    )
    await message.answer(text)


@router.message(Command("treasury"))
async def treasury(message: Message) -> None:
    remember_user(message)
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM treasury WHERE id = 1").fetchone()
        mayor = db.execute("SELECT full_name FROM users WHERE is_mayor = 1 LIMIT 1").fetchone()
    await message.answer(
        f"<b>Казна LimeWorld</b>\n"
        f"Баланс: {money(row['balance'])} {settings.currency}\n"
        f"Налог: {money(row['tax_amount'])} {settings.currency} в неделю\n"
        f"Мэр: {mayor['full_name'] if mayor else 'не назначен'}\n"
        "Пополнить казну: /donate 1000"
    )


@router.message(Command("donate"))
async def donate(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /donate 1000.")
        return
    if account["balance"] < amount:
        await message.answer("Недостаточно средств.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, account["user_id"]))
        db.execute("UPDATE treasury SET balance = balance + ?, updated_at = ? WHERE id = 1", (amount, now_iso()))
        add_transaction(db, "treasury_donate", amount, from_user_id=account["user_id"], comment="Донат в казну")
        db.commit()
    await message.answer(f"В казну отправлено {money(amount)} {settings.currency}.")


@router.message(Command("tax"))
async def tax(message: Message) -> None:
    account = remember_user(message)
    if account["tax_due_amount"] <= 0:
        await message.answer("Активных налоговых счетов нет.")
        return
    await message.answer(
        f"Вот счет за коммуналку и налоги: {money(account['tax_due_amount'])} {settings.currency}.\n"
        f"Оплатить: /paytax {account['tax_due_amount']}"
    )


@router.message(Command("paytax"))
async def paytax(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        amount = account["tax_due_amount"]
    pay_amount = min(amount, account["tax_due_amount"])
    if pay_amount <= 0:
        await message.answer("Активных налогов нет.")
        return
    if account["balance"] < pay_amount:
        await message.answer("Недостаточно средств для оплаты налога.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET balance = balance - ?, tax_due_amount = tax_due_amount - ? WHERE user_id = ?", (pay_amount, pay_amount, account["user_id"]))
        db.execute("UPDATE users SET tax_due_at = NULL WHERE user_id = ? AND tax_due_amount <= 0", (account["user_id"],))
        db.execute("UPDATE treasury SET balance = balance + ?, updated_at = ? WHERE id = 1", (pay_amount, now_iso()))
        add_transaction(db, "tax_pay", pay_amount, from_user_id=account["user_id"], comment="Оплата налога")
        db.commit()
    await message.answer(f"Налог оплачен: {money(pay_amount)} {settings.currency}.")


def is_mayor(user_id: int) -> bool:
    user = get_user(user_id)
    return bool(user and user["is_mayor"])


@router.message(Command("mayorwithdraw"))
async def mayorwithdraw(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    if not is_mayor(account["user_id"]):
        await message.answer("Эта команда доступна только мэру.")
        return
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /mayorwithdraw 1000.")
        return
    with closing(connect()) as db:
        row = db.execute("SELECT * FROM treasury WHERE id = 1").fetchone()
        if row["balance"] < amount:
            await message.answer("В казне недостаточно средств.")
            return
        db.execute("UPDATE treasury SET balance = balance - ?, updated_at = ? WHERE id = 1", (amount, now_iso()))
        db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, account["user_id"]))
        add_transaction(db, "mayor_withdraw", amount, to_user_id=account["user_id"], comment="Снятие из казны мэром")
        db.commit()
    await message.answer(f"Мэр снял из казны {money(amount)} {settings.currency}.")


@router.message(Command("settax"))
async def settax(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    if not is_mayor(account["user_id"]) and not is_admin_context(message):
        await message.answer("Налог может менять мэр или админ в спец-чате.")
        return
    amount = parse_amount((command.args or "").split()[0]) if command.args else None
    if amount is None:
        await message.answer("Формат: /settax 2000.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE treasury SET tax_amount = ?, tax_enabled = 1, updated_at = ? WHERE id = 1", (amount, now_iso()))
        db.commit()
    await message.answer(f"Недельный налог установлен: {money(amount)} {settings.currency}.")


@router.message(Command("billtax"))
async def billtax(message: Message) -> None:
    if not is_admin_context(message):
        await message.answer("Выставлять налоги можно только в спец-чате.")
        return
    due_at = now_utc() + timedelta(days=settings.tax_grace_days)
    with closing(connect()) as db:
        treasury_row = db.execute("SELECT * FROM treasury WHERE id = 1").fetchone()
        amount = treasury_row["tax_amount"]
        db.execute(
            """
            UPDATE users
            SET tax_due_amount = tax_due_amount + ?, tax_due_at = ?
            WHERE is_blocked = 0
            """,
            (amount, due_at.isoformat(timespec="seconds")),
        )
        db.commit()
    await message.answer(f"Налоговый счет выставлен всем активным клиентам: {money(amount)} до {due_at.date()}.")


@router.message(Command("casino"))
async def casino(message: Message) -> None:
    remember_user(message)
    await message.answer("Казино: /dice 100, /number 100 7, /blackjack 100.")


@router.message(Command("number"))
async def number_game(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Формат: /number 100 7. Угадай число от 1 до 10.")
        return
    bet = parse_amount(args[0])
    guess = parse_amount(args[1])
    if bet is None or guess is None or guess > 10 or account["balance"] < bet:
        await message.answer("Проверь ставку, число 1-10 и баланс.")
        return
    result = random.randint(1, 10)
    win = guess == result
    payout = bet * 7
    with closing(connect()) as db:
        if win:
            db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, account["user_id"]))
            add_transaction(db, "casino_win", payout, to_user_id=account["user_id"], comment='ООО "Бул казик"')
        else:
            db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (bet, account["user_id"]))
            add_transaction(db, "casino_loss", bet, from_user_id=account["user_id"], comment='ООО "Бнал"')
        db.commit()
    await message.answer(("Выигрыш" if win else "Проигрыш") + f". Выпало {result}. " + ('ООО "Бул казик"' if win else 'Списал ООО "Бнал"'))


@router.message(Command("dice"))
async def dice_game(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    args = (command.args or "").split()
    bet = parse_amount(args[0]) if args else None
    if bet is None or account["balance"] < bet:
        await message.answer("Формат: /dice 100. Нужно иметь ставку на балансе.")
        return
    roll = random.randint(1, 6)
    win = roll >= 5
    payout = bet * 2
    with closing(connect()) as db:
        if win:
            db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, account["user_id"]))
            add_transaction(db, "casino_win", payout, to_user_id=account["user_id"], comment='ООО "Бул казик"')
        else:
            db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (bet, account["user_id"]))
            add_transaction(db, "casino_loss", bet, from_user_id=account["user_id"], comment='ООО "Бнал"')
        db.commit()
    await message.answer(f"Кубик: {roll}. " + ("Выигрыш от ООО \"Бул казик\"." if win else "Списал ООО \"Бнал\"."))


def blackjack_score(cards: list[int]) -> int:
    total = sum(11 if card == 1 else min(card, 10) for card in cards)
    aces = cards.count(1)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


@router.message(Command("blackjack"))
async def blackjack(message: Message, command: CommandObject) -> None:
    account = remember_user(message)
    bet = parse_amount((command.args or "").split()[0]) if command.args else None
    if bet is None or account["balance"] < bet:
        await message.answer("Формат: /blackjack 100. Нужно иметь ставку на балансе.")
        return
    player = [random.randint(1, 13), random.randint(1, 13)]
    dealer = [random.randint(1, 13), random.randint(1, 13)]
    while blackjack_score(player) < 17:
        player.append(random.randint(1, 13))
    while blackjack_score(dealer) < 17:
        dealer.append(random.randint(1, 13))
    p_score = blackjack_score(player)
    d_score = blackjack_score(dealer)
    win = p_score <= 21 and (d_score > 21 or p_score > d_score)
    payout = bet * 2
    with closing(connect()) as db:
        if win:
            db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (payout, account["user_id"]))
            add_transaction(db, "casino_win", payout, to_user_id=account["user_id"], comment='ООО "Бул казик"')
        else:
            db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (bet, account["user_id"]))
            add_transaction(db, "casino_loss", bet, from_user_id=account["user_id"], comment='ООО "Бнал"')
        db.commit()
    await message.answer(
        f"21 очко: у тебя {p_score}, у дилера {d_score}. "
        + ("Выигрыш от ООО \"Бул казик\"." if win else "Списал ООО \"Бнал\".")
    )


@router.message(Command("panel"))
async def panel(message: Message) -> None:
    remember_user(message)
    if not is_admin_context(message):
        await message.answer("Админ-панель доступна только в назначенном чате.")
        return
    await message.answer(f"<b>{settings.bank_name}: админ-панель</b>", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(callback: CallbackQuery) -> None:
    if not is_admin_callback(callback):
        await callback.answer("Нет доступа.", show_alert=True)
        return
    action = callback.data.split(":", 1)[1]
    with closing(connect()) as db:
        if action == "stats":
            stats = db.execute(
                """
                SELECT COUNT(*) users_count, COALESCE(SUM(balance), 0) total_balance,
                       COALESCE(SUM(savings), 0) total_savings, COALESCE(SUM(loan), 0) total_loan,
                       COALESCE(SUM(crypto_balance), 0) crypto_total,
                       SUM(CASE WHEN is_blocked = 1 THEN 1 ELSE 0 END) frozen_count
                FROM users
                """
            ).fetchone()
            text = (
                f"<b>Статистика</b>\nКлиентов: {stats['users_count']}\n"
                f"Баланс: {money(stats['total_balance'])}\nВклады: {money(stats['total_savings'])}\n"
                f"Кредиты: {money(stats['total_loan'])}\nКрипта: {money(stats['crypto_total'])} LWC\n"
                f"Заморожено: {stats['frozen_count']}"
            )
        elif action == "top":
            rows = db.execute("SELECT full_name, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
            text = "<b>Топ игроков</b>\n" + "\n".join(f"{i}. {r['full_name']} - {money(r['balance'])}" for i, r in enumerate(rows, 1))
        elif action == "treasury":
            row = db.execute("SELECT * FROM treasury WHERE id = 1").fetchone()
            text = f"<b>Казна</b>\nБаланс: {money(row['balance'])}\nНалог: {money(row['tax_amount'])}"
        else:
            text = (
                "<b>Админ-команды</b>\n"
                "/check @user - проверка аккаунта\n"
                "/freeze @user причина - блокировка аккаунта\n"
                "/unfreeze @user - разморозка\n"
                "/verify @user - отметить проверенным\n"
                "/give, /take, /setbalance\n"
                "/setmayor @user - назначить мэра\n"
                "/billtax - выставить налог всем"
            )
    await callback.message.edit_text(text, reply_markup=admin_keyboard())
    await callback.answer()


async def admin_money_command(message: Message, command: CommandObject, mode: str) -> None:
    if not is_admin_context(message):
        await message.answer("Эта команда доступна только в назначенном админ-чате.")
        return
    target, args = resolve_target(message, command)
    if target is None or not args:
        await message.answer("Формат: команда @user сумма причина. Можно ответом на сообщение.")
        return
    amount = parse_amount(args[0])
    if amount is None:
        await message.answer("Сумма должна быть положительным числом.")
        return
    reason = " ".join(args[1:]) or "Админская операция"
    with closing(connect()) as db:
        if mode == "give":
            db.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, target["user_id"]))
            add_transaction(db, "admin_give", amount, to_user_id=target["user_id"], comment=reason)
            text = f"Начислено {money(amount)} игроку {target['full_name']}."
        elif mode == "take":
            db.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, target["user_id"]))
            add_transaction(db, "admin_take", amount, from_user_id=target["user_id"], comment=reason)
            text = f"Списано {money(amount)} у игрока {target['full_name']}."
        else:
            db.execute("UPDATE users SET balance = ? WHERE user_id = ?", (amount, target["user_id"]))
            add_transaction(db, "admin_setbalance", amount, to_user_id=target["user_id"], comment=reason)
            text = f"Баланс игрока {target['full_name']} установлен: {money(amount)}."
        db.commit()
    await message.answer(text)


@router.message(Command("give"))
async def give(message: Message, command: CommandObject) -> None:
    await admin_money_command(message, command, "give")


@router.message(Command("take"))
async def take(message: Message, command: CommandObject) -> None:
    await admin_money_command(message, command, "take")


@router.message(Command("setbalance"))
async def setbalance(message: Message, command: CommandObject) -> None:
    await admin_money_command(message, command, "set")


@router.message(Command("freeze", "banbank"))
async def freeze(message: Message, command: CommandObject) -> None:
    if not is_admin_context(message):
        await message.answer("Команда доступна только в назначенном админ-чате.")
        return
    target, args = resolve_target(message, command)
    if target is None:
        await message.answer("Кого заморозить? /freeze @user причина.")
        return
    reason = " ".join(args) or "заморозка админом"
    with closing(connect()) as db:
        db.execute("UPDATE users SET is_blocked = 1, block_reason = ?, block_until = NULL WHERE user_id = ?", (reason, target["user_id"]))
        add_transaction(db, "freeze", 0, from_user_id=target["user_id"], comment=reason)
        db.commit()
    await message.answer(f"Аккаунт {target['full_name']} заморожен.")


@router.message(Command("unfreeze", "unbanbank"))
async def unfreeze(message: Message, command: CommandObject) -> None:
    if not is_admin_context(message):
        await message.answer("Команда доступна только в назначенном админ-чате.")
        return
    target, _ = resolve_target(message, command)
    if target is None:
        await message.answer("Кого разморозить? /unfreeze @user.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET is_blocked = 0, block_reason = NULL, block_until = NULL WHERE user_id = ?", (target["user_id"],))
        add_transaction(db, "unfreeze", 0, to_user_id=target["user_id"], comment="Разморозка админом")
        db.commit()
    await message.answer(f"Аккаунт {target['full_name']} разморожен.")


@router.message(Command("verify"))
async def verify(message: Message, command: CommandObject) -> None:
    if not is_admin_context(message):
        await message.answer("Команда доступна только в назначенном админ-чате.")
        return
    target, _ = resolve_target(message, command)
    if target is None:
        await message.answer("Кого проверить? /verify @user.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET is_verified = 1 WHERE user_id = ?", (target["user_id"],))
        db.commit()
    await message.answer(f"Аккаунт {target['full_name']} отмечен как проверенный.")


@router.message(Command("check"))
async def check(message: Message, command: CommandObject) -> None:
    if not is_admin_context(message):
        await message.answer("Проверка аккаунта доступна только в назначенном админ-чате.")
        return
    target, _ = resolve_target(message, command)
    if target is None:
        await message.answer("Кого проверить? /check @user или ответом на сообщение.")
        return
    await message.answer(
        f"<b>Проверка аккаунта</b>\n"
        f"Клиент: {target['full_name']} (@{target['username'] or 'нет'})\n"
        f"ID: {target['user_id']}\n"
        f"Статус: {account_status(target)}\n"
        f"Баланс: {money(target['balance'])}\n"
        f"Вклад: {money(target['savings'])}\n"
        f"Кредит: {money(target['loan'])}\n"
        f"Z-ID: <code>{target['z_id']}</code>\n"
        f"Карта: ****{target['card_number'][-4:]}\n"
        f"Кошелек: <code>{target['crypto_wallet']}</code>\n"
        f"Регистрация: {target['registered_at'] or target['created_at']}"
    )


@router.message(Command("setmayor"))
async def setmayor(message: Message, command: CommandObject) -> None:
    if not is_admin_context(message):
        await message.answer("Мэра назначают только админы в спец-чате.")
        return
    target, _ = resolve_target(message, command)
    if target is None:
        await message.answer("Кого назначить мэром? /setmayor @user.")
        return
    with closing(connect()) as db:
        db.execute("UPDATE users SET is_mayor = 0")
        db.execute("UPDATE users SET is_mayor = 1 WHERE user_id = ?", (target["user_id"],))
        db.commit()
    await message.answer(f"{target['full_name']} теперь мэр.")


@router.message()
async def observe_chats(message: Message) -> None:
    logging.info("Message in chat_id=%s from user_id=%s", message.chat.id, message.from_user.id if message.from_user else None)
    if message.from_user:
        remember_user(message)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init_db()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    logging.info("%s started. Admin chat: %s", settings.bank_name, settings.admin_chat_id)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
