import os
import sqlite3
import logging
import threading
import uuid
from datetime import datetime

import requests
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

TOKEN = os.getenv("TOKEN")

WEBAPP_URL = os.getenv(
    "WEBAPP_URL",
    "https://abdusiraj609-a11y.github.io/Royal-Bingo-/"
)

DEPOSIT_PHONE = os.getenv("DEPOSIT_PHONE", "")

VERIFY_ET_API_KEY = os.getenv("VERIFY_ET_API_KEY", "")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

VERIFY_ET_BASE_URL = os.getenv(
    "VERIFY_ET_BASE_URL",
    "https://verify.et"
)

DEPOSIT_BANK = os.getenv(
    "DEPOSIT_BANK",
    "telebirr"
)

PORT = int(os.getenv("PORT", "5000"))

DB_PATH = os.getenv(
    "DB_PATH",
    "royal_bingo.db"
)

ENTRY_FEE = float(
    os.getenv("ENTRY_FEE", "10")
)

MAX_PLAYERS = int(
    os.getenv("MAX_PLAYERS", "500")
)

PRIZE_PERCENT = float(
    os.getenv("PRIZE_PERCENT", "80")
)

SUPPORT_USERNAME = os.getenv(
    "SUPPORT_USERNAME",
    "@RoyalBingoSupport"
)


if not TOKEN:
    raise RuntimeError(
        "TOKEN environment variable is not set"
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("royal-bingo")


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.Lock()


def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        # ----------------------------------------------------
        # USERS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,

                balance REAL DEFAULT 0,

                total_deposit REAL DEFAULT 0,
                total_withdraw REAL DEFAULT 0,
                total_won REAL DEFAULT 0,

                referral_code TEXT UNIQUE,
                referred_by TEXT,
                referral_earnings REAL DEFAULT 0,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ----------------------------------------------------
        # TRANSACTIONS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_id TEXT NOT NULL,

                type TEXT NOT NULL,

                amount REAL NOT NULL,

                status TEXT DEFAULT 'pending',

                reference TEXT,

                verify_request_id TEXT,

                note TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ----------------------------------------------------
        # WITHDRAWALS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_id TEXT NOT NULL,

                amount REAL NOT NULL,

                phone TEXT,

                status TEXT DEFAULT 'pending',

                note TEXT,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ----------------------------------------------------
        # DEPOSIT VERIFICATIONS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                telegram_id TEXT NOT NULL,

                reference TEXT UNIQUE,

                bank TEXT,

                amount REAL DEFAULT 0,

                verify_request_id TEXT,

                status TEXT DEFAULT 'pending',

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ----------------------------------------------------
        # GAME ROUNDS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                status TEXT DEFAULT 'waiting',

                players_count INTEGER DEFAULT 0,

                prize_pool REAL DEFAULT 0,

                payout_pool REAL DEFAULT 0,

                winners_count INTEGER DEFAULT 0,

                created_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                finished_at TIMESTAMP
            )
        """)

        # ----------------------------------------------------
        # GAME PLAYERS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS game_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,

                round_id INTEGER NOT NULL,

                telegram_id TEXT NOT NULL,

                entry_fee REAL NOT NULL,

                joined_at TIMESTAMP
                    DEFAULT CURRENT_TIMESTAMP,

                UNIQUE(round_id, telegram_id)
            )
        """)

        conn.commit()
        conn.close()

    logger.info(
        "Database initialized: %s",
        DB_PATH
    )


# ============================================================
# USERS
# ============================================================

def ensure_user(
    telegram_id,
    username=None,
    first_name=None,
    referred_by=None
):

    telegram_id = str(telegram_id)

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT telegram_id
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        )

        exists = cur.fetchone()

        if not exists:

            referral_code = (
                "RB" + telegram_id
            )

            cur.execute(
                """
                INSERT INTO users (
                    telegram_id,
                    username,
                    first_name,
                    referral_code,
                    referred_by
                )

                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    username,
                    first_name,
                    referral_code,
                    referred_by
                )
            )

        else:

            cur.execute(
                """
                UPDATE users

                SET username = ?,
                    first_name = ?

                WHERE telegram_id = ?
                """,
                (
                    username,
                    first_name,
                    telegram_id
                )
            )

        conn.commit()
        conn.close()


def get_user(telegram_id):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM users
            WHERE telegram_id = ?
            """,
            (str(telegram_id),)
        )

        user = cur.fetchone()

        conn.close()

    return user


def get_balance(telegram_id):

    user = get_user(telegram_id)

    if not user:
        return 0.0

    return float(user["balance"])


# ============================================================
# BALANCE
# ============================================================

def credit_balance(
    telegram_id,
    amount,
    transaction_type,
    note="",
    reference=None,
    verify_request_id=None
):

    amount = float(amount)

    if amount <= 0:
        return None

    telegram_id = str(telegram_id)

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users

            SET balance = balance + ?,

                total_deposit =
                    CASE
                        WHEN ? = 'deposit'
                        THEN total_deposit + ?
                        ELSE total_deposit
                    END,

                total_won =
                    CASE
                        WHEN ? = 'prize'
                        THEN total_won + ?
                        ELSE total_won
                    END

            WHERE telegram_id = ?
            """,
            (
                amount,
                transaction_type,
                amount,
                transaction_type,
                amount,
                telegram_id
            )
        )

        cur.execute(
            """
            INSERT INTO transactions (
                telegram_id,
                type,
                amount,
                status,
                reference,
                verify_request_id,
                note
            )

            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                transaction_type,
                amount,
                "completed",
                reference,
                verify_request_id,
                note
            )
        )

        conn.commit()

        cur.execute(
            """
            SELECT balance
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        )

        row = cur.fetchone()

        conn.close()

    if not row:
        return None

    return float(row["balance"])


def debit_balance(
    telegram_id,
    amount,
    transaction_type,
    note=""
):

    amount = float(amount)

    if amount <= 0:
        return None

    telegram_id = str(telegram_id)

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT balance
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        )

        row = cur.fetchone()

        if not row:

            conn.close()

            return None

        balance = float(row["balance"])

        if balance < amount:

            conn.close()

            return False

        cur.execute(
            """
            UPDATE users

            SET balance = balance - ?,

                total_withdraw =
                    CASE
                        WHEN ? = 'withdraw'
                        THEN total_withdraw + ?
                        ELSE total_withdraw
                    END

            WHERE telegram_id = ?
            """,
            (
                amount,
                transaction_type,
                amount,
                telegram_id
            )
        )

        cur.execute(
            """
            INSERT INTO transactions (
                telegram_id,
                type,
                amount,
                status,
                note
            )

            VALUES (?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                transaction_type,
                amount,
                "completed",
                note
            )
        )

        conn.commit()

        cur.execute(
            """
            SELECT balance
            FROM users
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        )

        new_row = cur.fetchone()

        conn.close()

    return float(new_row["balance"])


# ============================================================
# TELEGRAM MENU
# ============================================================

def main_menu():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🎮 ጨዋታ",
                web_app=WebAppInfo(
                    url=WEBAPP_URL
                )
            )
        ],

        [
            InlineKeyboardButton(
                "💰 ገንዘብ አስገባ",
                callback_data="deposit"
            ),

            InlineKeyboardButton(
                "💸 ገንዘብ አውጣ",
                callback_data="withdraw"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 ግብዣ",
                callback_data="referral"
            ),

            InlineKeyboardButton(
                "🆘 ድጋፍ",
                callback_data="support"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    referred_by = None

    if context.args:

        referred_by = context.args[0]

    ensure_user(
        user.id,
        user.username,
        user.first_name,
        referred_by
    )

    balance = get_balance(user.id)

    text = f"""
🎱 <b>ሮያል ቢንጎ</b>

እንኳን ወደ Royal Bingo በደህና መጡ! 🎉

💰 የእርስዎ ቀሪ ሂሳብ:
<b>{balance:.2f} ETB</b>

🎟️ የጨዋታ መግቢያ:
<b>{ENTRY_FEE:.2f} ETB</b>

ከታች ያለውን ምናሌ ይጠቀሙ።
"""

    await update.message.reply_text(
        text,
        reply_markup=main_menu(),
        parse_mode="HTML"
    )


# ============================================================
# DEPOSIT INSTRUCTIONS
# ============================================================

async def show_deposit(
    query,
    context
):

    text = f"""
💰 <b>ገንዘብ አስገባ</b>

1️⃣ ወደዚህ የክፍያ ቁጥር ገንዘብ ይላኩ፦

📱 <code>{DEPOSIT_PHONE}</code>

2️⃣ ክፍያው ከተጠናቀቀ በኋላ የTransaction Number / Reference ይላኩ።

3️⃣ Royal Bingo ክፍያውን በVerify.ET ያረጋግጣል።

4️⃣ ከተረጋገጠ በኋላ ገንዘቡ በራስ-ሰር ወደ ሂሳብዎ ይጨመራል።

🏦 Bank:
<b>{DEPOSIT_BANK}</b>
"""

    await query.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# WITHDRAW INSTRUCTIONS
# ============================================================

async def show_withdraw(
    query,
    user_id
):

    balance = get_balance(user_id)

    text = f"""
💸 <b>ገንዘብ አውጣ</b>

💰 የአሁኑ ሂሳብ:
<b>{balance:.2f} ETB</b>

የሚፈልጉትን የWithdrawal መጠን ይላኩ።

ምሳሌ:
<code>100</code>

ከዚያ የሚቀበሉበትን የስልክ ቁጥር ይላኩ።
"""

    await query.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# REFERRAL
# ============================================================

async def show_referral(
    query,
    context,
    user_id
):

    user = get_user(user_id)

    code = user["referral_code"]

    bot_username = context.bot.username

    link = (
        f"https://t.me/{bot_username}"
        f"?start={code}"
    )

    earnings = float(
        user["referral_earnings"]
    )

    text = f"""
👥 <b>ግብዣ</b>

ጓደኞችዎን Royal Bingo እንዲጫወቱ ይጋብዙ።

🔗 የእርስዎ የግብዣ ሊንክ:

<code>{link}</code>

💎 የReferral ገቢ:

<b>{earnings:.2f} ETB</b>
"""

    await query.message.reply_text(
        text,
        parse_mode="HTML"
    )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(
        user.id,
        user.username,
        user.first_name
    )

    if query.data == "deposit":

        await show_deposit(
            query,
            context
        )

    elif query.data == "withdraw":

        await show_withdraw(
            query,
            user.id
        )

    elif query.data == "referral":

        await show_referral(
            query,
            context,
            user.id
        )

    elif query.data == "support":

        await query.message.reply_text(
            f"""
🆘 <b>ድጋፍ</b>

ችግር ካጋጠመዎት የSupport ቡድናችንን ያነጋግሩ።

📩 {SUPPORT_USERNAME}
""",
            parse_mode="HTML"
        )


# ============================================================
# VERIFY.ET
# ============================================================

def verify_payment(
    reference,
    bank=None
):

    if not VERIFY_ET_API_KEY:

        return {
            "success": False,
            "error": "VERIFY_ET_API_KEY is not configured"
        }

    bank = bank or DEPOSIT_BANK

    url = (
        VERIFY_ET_BASE_URL
        + "/api/verify"
    )

    payload = {
        "bank": bank,
        "reference": reference,
    }

    # Telebirr uses transactionNumber.
    if bank == "telebirr":

        payload = {
            "bank": "telebirr",
            "transactionNumber": reference,
            "settlementAccount": DEPOSIT_PHONE
        }

    # MPESA
    elif bank == "mpesa":

        payload = {
            "bank": "mpesa",
            "transactionNumber": reference,
            "settlementAccount": DEPOSIT_PHONE
        }

    # CBE Birr
    elif bank == "cbebirr":

        payload = {
            "bank": "cbebirr",
            "receiptNumber": reference,
            "phoneNumber": DEPOSIT_PHONE,
            "settlementAccount": DEPOSIT_PHONE
        }

    # CBE
    elif bank == "cbe":

        suffix = os.getenv(
            "DEPOSIT_ACCOUNT_SUFFIX",
            ""
        )

        payload = {
            "bank": "cbe",
            "referenceNumber": reference,
            "accountSuffix": suffix
        }

    # BOA
    elif bank == "boa":

        suffix = os.getenv(
            "DEPOSIT_ACCOUNT_SUFFIX",
            ""
        )

        payload = {
            "bank": "boa",
            "referenceNumber": reference,
            "accountSuffix": suffix
        }

    headers = {
        "Content-Type": "application/json",
        "x-api-key": VERIFY_ET_API_KEY,
        "Idempotency-Key":
            "royal-bingo-" + str(uuid.uuid4())
    }

    try:

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            params={"waitMs": 5000},
            timeout=20
        )

        try:
            data = response.json()

        except Exception:

            return {
                "success": False,
                "error": "Invalid Verify.ET response",
                "http_status":
                    response.status_code
            }

        return {
            "success":
                bool(
                    data.get("success")
                ),

            "http_status":
                response.status_code,

            "data":
                data
        }

    except requests.RequestException as exc:

        logger.exception(
            "Verify.ET request failed"
        )

        return {
            "success": False,
            "error": str(exc)
        }


# ============================================================
# DEPOSIT API
# ============================================================

@app.post("/api/deposit/verify")
def api_deposit_verify():

    data = request.get_json(
        silent=True
    ) or {}

    telegram_id = data.get(
        "telegram_id"
    )

    reference = data.get(
        "reference"
    )

    bank = data.get(
        "bank",
        DEPOSIT_BANK
    )

    if not telegram_id:

        return jsonify({
            "success": False,
            "error": "telegram_id required"
        }), 400

    if not reference:

        return jsonify({
            "success": False,
            "error": "reference required"
        }), 400

    ensure_user(telegram_id)

    reference = str(reference).strip()

    # --------------------------------------------------------
    # Prevent duplicate deposits
    # --------------------------------------------------------

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM deposits
            WHERE reference = ?
            """,
            (reference,)
        )

        existing = cur.fetchone()

        conn.close()

    if existing:

        return jsonify({
            "success": False,
            "error": "transaction_already_used"
        }), 409

    # --------------------------------------------------------
    # Verify
    # --------------------------------------------------------

    result = verify_payment(
        reference,
        bank
    )

    if not result.get("success"):

        return jsonify({
            "success": False,
            "error": result.get(
                "error",
                "verification_failed"
            ),
            "verify": result
        }), 400

    raw = result.get(
        "data",
        {}
    )

    verification = raw.get(
        "verification",
        {}
    )

    status = verification.get(
        "status"
    )

    verified = verification.get(
        "verified",
        False
    )

    request_id = raw.get(
        "requestId"
    )

    items = raw.get(
        "data",
        []
    )

    amount = 0

    if items:

        amount = float(
            items[0].get(
                "amount",
                0
            ) or 0
        )

    # --------------------------------------------------------
    # Completed
    # --------------------------------------------------------

    if verified and status == "success":

        if amount <= 0:

            return jsonify({
                "success": False,
                "error": "invalid_verified_amount"
            }), 400

        # Settlement account protection
        if items:

            match = items[0].get(
                "settlementAccountMatch"
            )

            if match:

                if not match.get(
                    "matched",
                    False
                ):

                    return jsonify({
                        "success": False,
                        "error":
                            "receiver_account_mismatch"
                    }), 400

        with db_lock:

            conn = get_db()
            cur = conn.cursor()

            cur.execute(
                """
                INSERT INTO deposits (
                    telegram_id,
                    reference,
                    bank,
                    amount,
                    verify_request_id,
                    status
                )

                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(telegram_id),
                    reference,
                    bank,
                    amount,
                    request_id,
                    "completed"
                )
            )

            conn.commit()

            conn.close()

        new_balance = credit_balance(
            telegram_id,
            amount,
            "deposit",
            "Verified deposit",
            reference,
            request_id
        )

        return jsonify({

            "success": True,

            "status": "completed",

            "amount": amount,

            "balance": new_balance,

            "reference": reference,

            "request_id": request_id
        })

    # --------------------------------------------------------
    # Pending / queued
    # --------------------------------------------------------

    return jsonify({

        "success": True,

        "status": "pending",

        "request_id": request_id,

        "message":
            "Verification is still processing."
    }), 202


# ============================================================
# WALLET
# ============================================================

@app.get("/api/wallet")
def api_wallet():

    telegram_id = request.args.get(
        "telegram_id"
    )

    if not telegram_id:

        return jsonify({
            "success": False,
            "error": "telegram_id required"
        }), 400

    user = get_user(telegram_id)

    if not user:

        return jsonify({
            "success": False,
            "error": "user not found"
        }), 404

    return jsonify({

        "success": True,

        "telegram_id":
            str(telegram_id),

        "username":
            user["username"],

        "balance":
            float(user["balance"]),

        "total_deposit":
            float(user["total_deposit"]),

        "total_withdraw":
            float(user["total_withdraw"]),

        "total_won":
            float(user["total_won"]),

        "referral_earnings":
            float(
                user["referral_earnings"]
            )
    })


# ============================================================
# USER
# ============================================================

@app.post("/api/user")
def api_user():

    data = request.get_json(
        silent=True
    ) or {}

    telegram_id = data.get(
        "telegram_id"
    )

    if not telegram_id:

        return jsonify({
            "success": False,
            "error": "telegram_id required"
        }), 400

    ensure_user(
        telegram_id,
        data.get("username"),
        data.get("first_name")
    )

    user = get_user(
        telegram_id
    )

    return jsonify({

        "success": True,

        "telegram_id":
            str(telegram_id),

        "username":
            user["username"],

        "first_name":
            user["first_name"],

        "balance":
            float(user["balance"]),

        "entry_fee":
            ENTRY_FEE,

        "max_players":
            MAX_PLAYERS
    })


# ============================================================
# GAME CONFIG
# ============================================================

@app.get("/api/game/config")
def game_config():

    return jsonify({

        "success": True,

        "entry_fee":
            ENTRY_FEE,

        "max_players":
            MAX_PLAYERS,

        "prize_percent":
            PRIZE_PERCENT,

        "currency":
            "ETB"
    })


# ============================================================
# JOIN GAME
# ============================================================

@app.post("/api/game/join")
def game_join():

    data = request.get_json(
        silent=True
    ) or {}

    telegram_id = data.get(
        "telegram_id"
    )

    round_id = data.get(
        "round_id"
    )

    if not telegram_id:

        return jsonify({
            "success": False,
            "error": "telegram_id required"
        }), 400

    ensure_user(telegram_id)

    # --------------------------------------------------------
    # Find / create round
    # --------------------------------------------------------

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        if round_id:

            cur.execute(
                """
                SELECT *
                FROM game_rounds
                WHERE id = ?
                """,
                (round_id,)
            )

        else:

            cur.execute(
                """
                SELECT *
                FROM game_rounds
                WHERE status = 'waiting'
                ORDER BY id DESC
                LIMIT 1
                """
            )

        game_round = cur.fetchone()

        if not game_round:

            cur.execute(
                """
                INSERT INTO game_rounds (
                    status,
                    players_count,
                    prize_pool
                )

                VALUES (?, ?, ?)
                """,
                (
                    "waiting",
                    0,
                    0
                )
            )

            round_id = cur.lastrowid

            cur.execute(
                """
                SELECT *
                FROM game_rounds
                WHERE id = ?
                """,
                (round_id,)
            )

            game_round = cur.fetchone()

        else:

            round_id = game_round["id"]

        # ----------------------------------------------------
        # Already joined?
        # ----------------------------------------------------

        cur.execute(
            """
            SELECT id
            FROM game_players

            WHERE round_id = ?
            AND telegram_id = ?
            """,
            (
                round_id,
                str(telegram_id)
            )
        )

        already = cur.fetchone()

        if already:

            conn.close()

            return jsonify({
                "success": True,
                "already_joined": True,
                "round_id": round_id,
                "balance":
                    get_balance(telegram_id)
            })

        # ----------------------------------------------------
        # Check balance INSIDE transaction lock
        # ----------------------------------------------------

        cur.execute(
            """
            SELECT balance
            FROM users
            WHERE telegram_id = ?
            """,
            (str(telegram_id),)
        )

        user = cur.fetchone()

        balance = float(
            user["balance"]
        )

        if balance < ENTRY_FEE:

            conn.close()

            return jsonify({

                "success": False,

                "error":
                    "insufficient_balance",

                "balance":
                    balance,

                "entry_fee":
                    ENTRY_FEE
            }), 400

        # ----------------------------------------------------
        # Deduct entry
        # ----------------------------------------------------

        cur.execute(
            """
            UPDATE users

            SET balance =
                balance - ?

            WHERE telegram_id = ?
            """,
            (
                ENTRY_FEE,
                str(telegram_id)
            )
        )

        cur.execute(
            """
            INSERT INTO transactions (
                telegram_id,
                type,
                amount,
                status,
                note
            )

            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(telegram_id),
                "game_entry",
                ENTRY_FEE,
                "completed",
                f"Round {round_id}"
            )
        )

        cur.execute(
            """
            INSERT INTO game_players (
                round_id,
                telegram_id,
                entry_fee
            )

            VALUES (?, ?, ?)
            """,
            (
                round_id,
                str(telegram_id),
                ENTRY_FEE
            )
        )

        cur.execute(
            """
            UPDATE game_rounds

            SET players_count =
                    players_count + 1,

                prize_pool =
                    prize_pool + ?

            WHERE id = ?
            """,
            (
                ENTRY_FEE,
                round_id
            )
        )

        conn.commit()

        cur.execute(
            """
            SELECT balance
            FROM users
            WHERE telegram_id = ?
            """,
            (str(telegram_id),)
        )

        new_balance = float(
            cur.fetchone()["balance"]
        )

        conn.close()

    return jsonify({

        "success": True,

        "round_id":
            round_id,

        "entry_fee":
            ENTRY_FEE,

        "balance":
            new_balance
    })


# ============================================================
# PRIZE
# ============================================================

@app.post("/api/prize")
def api_prize():

    admin_token = request.headers.get(
        "X-Admin-Token"
    )

    if not ADMIN_TOKEN:

        return jsonify({
            "success": False,
            "error":
                "ADMIN_TOKEN not configured"
        }), 500

    if admin_token != ADMIN_TOKEN:

        return jsonify({
            "success": False,
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    telegram_id = data.get(
        "telegram_id"
    )

    amount = data.get(
        "amount"
    )

    round_id = data.get(
        "round_id"
    )

    if not telegram_id or amount is None:

        return jsonify({
            "success": False,
            "error":
                "telegram_id and amount required"
        }), 400

    try:

        amount = float(amount)

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "error": "invalid amount"
        }), 400

    if amount <= 0:

        return jsonify({
            "success": False,
            "error":
                "amount must be positive"
        }), 400

    ensure_user(telegram_id)

    new_balance = credit_balance(
        telegram_id,
        amount,
        "prize",
        f"Royal Bingo prize - round {round_id}"
    )

    return jsonify({

        "success": True,

        "telegram_id":
            str(telegram_id),

        "prize":
            amount,

        "balance":
            new_balance,

        "round_id":
            round_id
    })


# ============================================================
# ROUND PRIZE CALCULATION
# ============================================================

@app.get("/api/game/round/<int:round_id>")
def get_round(round_id):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM game_rounds
            WHERE id = ?
            """,
            (round_id,)
        )

        round_data = cur.fetchone()

        if not round_data:

            conn.close()

            return jsonify({
                "success": False,
                "error": "round not found"
            }), 404

        conn.close()

    prize_pool = float(
        round_data["prize_pool"]
    )

    payout_pool = (
        prize_pool *
        PRIZE_PERCENT /
        100
    )

    return jsonify({

        "success": True,

        "round_id":
            round_id,

        "status":
            round_data["status"],

        "players_count":
            round_data["players_count"],

        "prize_pool":
            prize_pool,

        "payout_pool":
            payout_pool,

        "prize_percent":
            PRIZE_PERCENT
    })


# ============================================================
# WITHDRAW REQUEST
# ============================================================

@app.post("/api/withdraw")
def api_withdraw():

    data = request.get_json(
        silent=True
    ) or {}

    telegram_id = data.get(
        "telegram_id"
    )

    amount = data.get(
        "amount"
    )

    phone = data.get(
        "phone"
    )

    if not telegram_id:
        return jsonify({
            "success": False,
            "error":
                "telegram_id required"
        }), 400

    if amount is None:
        return jsonify({
            "success": False,
            "error":
                "amount required"
        }), 400

    if not phone:
        return jsonify({
            "success": False,
            "error":
                "phone required"
        }), 400

    try:

        amount = float(amount)

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "error":
                "invalid amount"
        }), 400

    if amount <= 0:

        return jsonify({
            "success": False,
            "error":
                "amount must be positive"
        }), 400

    ensure_user(telegram_id)

    # --------------------------------------------------------
    # Reserve balance
    # --------------------------------------------------------

    new_balance = debit_balance(
        telegram_id,
        amount,
        "withdraw",
        "Withdrawal request"
    )

    if new_balance is False:

        return jsonify({

            "success": False,

            "error":
                "insufficient_balance",

            "balance":
                get_balance(telegram_id)
        }), 400

    if new_balance is None:

        return jsonify({
            "success": False,
            "error":
                "user not found"
        }), 404

    # --------------------------------------------------------
    # Create withdrawal
    # --------------------------------------------------------

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO withdrawals (
                telegram_id,
                amount,
                phone,
                status
            )

            VALUES (?, ?, ?, ?)
            """,
            (
                str(telegram_id),
                amount,
                phone,
                "pending"
            )
        )

        withdrawal_id = cur.lastrowid

        conn.commit()
        conn.close()

    return jsonify({

        "success": True,

        "withdrawal_id":
            withdrawal_id,

        "amount":
            amount,

        "phone":
            phone,

        "status":
            "pending",

        "balance":
            new_balance
    })


# ============================================================
# ADMIN WITHDRAWALS
# ============================================================

@app.get("/api/admin/withdrawals")
def admin_withdrawals():

    token = request.headers.get(
        "X-Admin-Token"
    )

    if token != ADMIN_TOKEN:

        return jsonify({
            "success": False,
            "error": "unauthorized"
        }), 401

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM withdrawals
            ORDER BY id DESC
            LIMIT 100
            """
        )

        rows = cur.fetchall()

        conn.close()

    withdrawals = []

    for row in rows:

        withdrawals.append(
            dict(row)
        )

    return jsonify({

        "success": True,

        "withdrawals":
            withdrawals
    })


# ============================================================
# ADMIN WITHDRAWAL STATUS
# ============================================================

@app.post("/api/admin/withdrawal/<int:withdrawal_id>")
def admin_withdrawal_update(
    withdrawal_id
):

    token = request.headers.get(
        "X-Admin-Token"
    )

    if token != ADMIN_TOKEN:

        return jsonify({
            "success": False,
            "error": "unauthorized"
        }), 401

    data = request.get_json(
        silent=True
    ) or {}

    status = data.get(
        "status"
    )

    if status not in [
        "approved",
        "completed",
        "rejected"
    ]:

        return jsonify({
            "success": False,
            "error":
                "invalid status"
        }), 400

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE id = ?
            """,
            (withdrawal_id,)
        )

        withdrawal = cur.fetchone()

        if not withdrawal:

            conn.close()

            return jsonify({
                "success": False,
                "error":
                    "withdrawal not found"
            }), 404

        old_status = withdrawal["status"]

        # ----------------------------------------------------
        # If rejected, return reserved money.
        # ----------------------------------------------------

        if (
            status == "rejected"
            and old_status
                not in ["rejected", "completed"]
        ):

            telegram_id = withdrawal[
                "telegram_id"
            ]

            amount = float(
                withdrawal["amount"]
            )

            cur.execute(
                """
                UPDATE users

                SET balance =
                    balance + ?,

                    total_withdraw =
                    CASE
                        WHEN total_withdraw >= ?
                        THEN total_withdraw - ?
                        ELSE 0
                    END

                WHERE telegram_id = ?
                """,
                (
                    amount,
                    amount,
                    amount,
                    telegram_id
                )
            )

            cur.execute(
                """
                INSERT INTO transactions (
                    telegram_id,
                    type,
                    amount,
                    status,
                    note
                )

                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    telegram_id,
                    "withdraw_refund",
                    amount,
                    "completed",
                    f"Withdrawal {withdrawal_id} rejected"
                )
            )

        cur.execute(
            """
            UPDATE withdrawals

            SET status = ?

            WHERE id = ?
            """,
            (
                status,
                withdrawal_id
            )
        )

        conn.commit()
        conn.close()

    return jsonify({

        "success": True,

        "withdrawal_id":
            withdrawal_id,

        "status":
            status
    })


# ============================================================
# HEALTH
# ============================================================

@app.get("/")
def health():

    return jsonify({

        "success": True,

        "service":
            "Royal Bingo Server",

        "status":
            "online",

        "webapp":
            WEBAPP_URL,

        "entry_fee":
            ENTRY_FEE,

        "max_players":
            MAX_PLAYERS
    })


# ============================================================
# FLASK
# ============================================================

def run_web():

    logger.info(
        "Royal Bingo API running on port %s",
        PORT
    )

    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )


# ============================================================
# BOT
# ============================================================

def main():

    init_db()

    web_thread = threading.Thread(
        target=run_web,
        daemon=True
    )

    web_thread.start()

    application = (
        Application
        .builder()
        .token(TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    logger.info(
        "🎱 Royal Bingo bot started"
    )

    application.run_polling(
        drop_pending_updates=True
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
