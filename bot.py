"""
ROYAL BINGO — Server (Bot + Game API)
=====================================

ملف واحد يشغّل:
1. Telegram Bot
2. Round Engine
3. Flask Game API

القواعد:
- أول Bingo يوقف الجولة فورًا.
- جميع اللاعبين في الجولة يرون نفس تسلسل الكرات.
- Prize Pool = 80% من مجموع Entry Stake المدفوع في الجولة.
- عند التعادل في نفس الكرة، يتم تقسيم Prize Pool بالتساوي بين
  جميع الكرتيلات الفائزة.
- الرصيد والنتيجة والفوز يقررها السيرفر فقط.
"""

import os
import json
import time
import random
import hmac
import hashlib
import logging
import threading
import urllib.parse
import asyncio
import uuid
import requests
import sqlite3

from datetime import datetime

from flask import Flask, request, jsonify

from telegram import (
    Update,
    Bot,
    ReplyKeyboardMarkup,
    KeyboardButton,
    WebAppInfo,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

log = logging.getLogger("royal_bingo_server")


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

ADMIN_IDS = {
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip()
}

WEBAPP_URL = os.environ.get(
    "WEBAPP_URL",
    "https://example.com"
)

SUPPORT_CONTACT = os.environ.get(
    "SUPPORT_CONTACT",
    "@your_support_username"
)

PORT = int(
    os.environ.get("PORT", "5000")
)


# ============================================================
# TELEBIRR / DEPOSIT SETTINGS
# ============================================================

DEPOSIT_METHOD = "Telebirr"

DEPOSIT_ACCOUNT_NAME = "Abdurahman"

DEPOSIT_ACCOUNT_NUMBER = "0993946560"


# ============================================================
# VERIFY.ET
# ============================================================

VERIFY_ET_API_KEY = os.environ.get(
    "VERIFY_ET_API_KEY",
    ""
)

VERIFY_ET_BASE_URL = "https://verify.et"

VERIFY_ET_WAIT_MS = int(
    os.environ.get("VERIFY_ET_WAIT_MS", "8000")
)


# ============================================================
# DATABASE
# ============================================================

DB_PATH = os.environ.get(
    "DB_PATH",
    "royal_bingo.db"
)


_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(
        DB_PATH,
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    return conn


# ============================================================
# GAME ECONOMY
# ============================================================

ENTRY_STAKE = int(
    os.environ.get("ENTRY_STAKE", "10")
)

MAX_CARTELAS = int(
    os.environ.get("MAX_CARTELAS", "4")
)

COLLECT_SECONDS = int(
    os.environ.get("COLLECT_SECONDS", "45")
)

BALL_DRAW_SECONDS = float(
    os.environ.get("BALL_DRAW_SECONDS", "2.0")
)

PAYOUT_RATIO = float(
    os.environ.get("PAYOUT_RATIO", "0.80")
)


# ============================================================
# CONVERSATION STATES
# ============================================================

DEP_AMOUNT = 1
DEP_TXNID = 2

WD_AMOUNT = 3
WD_PHONE = 4


# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():

    conn = db()

    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        balance INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS deposits(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        amount INTEGER NOT NULL,
        method TEXT NOT NULL,
        txn_id TEXT,
        photo_file_id TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        admin_msg_chat_id INTEGER,
        admin_msg_id INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS withdrawals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        amount INTEGER NOT NULL,
        method TEXT NOT NULL,
        account TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        admin_msg_chat_id INTEGER,
        admin_msg_id INTEGER,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS rounds(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT NOT NULL DEFAULT 'collecting',
        close_at REAL NOT NULL,
        ball_sequence TEXT,
        active_started_at REAL,
        next_ball_at REAL,
        called_balls TEXT NOT NULL DEFAULT '[]',
        prize_pool INTEGER NOT NULL DEFAULT 0,
        finished_at REAL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS round_entries(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        grid TEXT NOT NULL,
        stake_paid INTEGER NOT NULL,
        is_winner INTEGER NOT NULL DEFAULT 0,
        payout INTEGER NOT NULL DEFAULT 0
    );
    """)

    conn.commit()

    # --------------------------------------------------------
    # منع تكرار Transaction ID
    # --------------------------------------------------------

    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            idx_deposits_txn_id_unique
            ON deposits(txn_id)
            WHERE txn_id IS NOT NULL
              AND txn_id != ''
        """)

        conn.commit()

    except sqlite3.IntegrityError:

        log.exception(
            "Could not create unique transaction index. "
            "Existing duplicate txn_id values may exist."
        )

    conn.close()


# ============================================================
# USERS / BALANCE
# ============================================================

def get_or_create_user(user_id: int, username: str) -> int:

    conn = db()

    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if row is None:

        conn.execute(
            """
            INSERT INTO users(user_id, username, balance)
            VALUES (?, ?, 0)
            """,
            (
                user_id,
                username or "",
            )
        )

        conn.commit()

        balance = 0

    else:

        conn.execute(
            """
            UPDATE users
            SET username=?
            WHERE user_id=?
            """,
            (
                username or "",
                user_id,
            )
        )

        conn.commit()

        balance = row["balance"]

    conn.close()

    return balance


def get_balance(user_id: int) -> int:

    conn = db()

    row = conn.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    conn.close()

    if row is None:
        return 0

    return int(row["balance"])


# ============================================================
# TELEBIRR VERIFICATION
# ============================================================

def verify_telebirr_payment(transaction_number: str):

    if not VERIFY_ET_API_KEY:

        return {
            "ok": False,
            "reason": "verify_not_configured",
            "fallback_to_manual": True,
        }


    headers = {
        "Content-Type": "application/json",
        "x-api-key": VERIFY_ET_API_KEY,
        "Idempotency-Key": (
            f"royal-bingo-{uuid.uuid4()}"
        ),
    }


    body = {
        "bank": "telebirr",
        "transactionNumber": transaction_number,
        "settlementAccount": DEPOSIT_ACCOUNT_NUMBER,
    }


    try:

        resp = requests.post(
            f"{VERIFY_ET_BASE_URL}/api/verify"
            f"?waitMs={VERIFY_ET_WAIT_MS}",
            json=body,
            headers=headers,
            timeout=(VERIFY_ET_WAIT_MS / 1000) + 5,
        )

    except requests.RequestException as e:

        log.warning(
            "Verify.ET network error: %s",
            e
        )

        return {
            "ok": False,
            "reason": "network_error",
            "fallback_to_manual": True,
        }


    if resp.status_code == 202:

        return {
            "ok": False,
            "reason": "still_processing",
            "fallback_to_manual": True,
        }


    if resp.status_code == 401:

        log.error(
            "Verify.ET invalid API key!"
        )

        return {
            "ok": False,
            "reason": "verify_misconfigured",
            "fallback_to_manual": True,
        }


    if resp.status_code == 402:

        log.error(
            "Verify.ET verification credits exhausted!"
        )

        return {
            "ok": False,
            "reason": "verify_credits_exhausted",
            "fallback_to_manual": True,
        }


    if resp.status_code == 429:

        return {
            "ok": False,
            "reason": "rate_limited",
            "fallback_to_manual": True,
        }


    if resp.status_code != 200:

        log.warning(
            "Verify.ET unexpected status %s: %s",
            resp.status_code,
            resp.text[:300],
        )

        return {
            "ok": False,
            "reason": f"http_{resp.status_code}",
            "fallback_to_manual": True,
        }


    try:

        payload = resp.json()

    except ValueError:

        return {
            "ok": False,
            "reason": "bad_response",
            "fallback_to_manual": True,
        }


    if not payload.get("success"):

        return {
            "ok": False,
            "reason": "verify_failed",
            "fallback_to_manual": True,
        }


    items = payload.get("data") or []

    if not items:

        return {
            "ok": False,
            "reason": "no_data",
            "fallback_to_manual": True,
        }


    item = items[0]


    if not item.get("verified"):

        return {
            "ok": False,
            "reason": "not_verified",
            "fallback_to_manual": False,
        }


    confirmation = (
        item.get("confirmationHistory")
        or {}
    )


    if confirmation.get("confirmedBefore"):

        return {
            "ok": False,
            "reason": "duplicate_transaction",
            "fallback_to_manual": False,
        }


    settlement_match = (
        item.get("settlementAccountMatch")
    )


    if (
        settlement_match
        and settlement_match.get("matched") is False
    ):

        return {
            "ok": False,
            "reason": "wrong_recipient",
            "fallback_to_manual": False,
        }


    amount = item.get("amount")

    if amount is None:

        return {
            "ok": False,
            "reason": "missing_verified_amount",
            "fallback_to_manual": False,
        }


    try:

        verified_amount = int(amount)

    except (TypeError, ValueError):

        return {
            "ok": False,
            "reason": "invalid_verified_amount",
            "fallback_to_manual": False,
        }


    if verified_amount <= 0:

        return {
            "ok": False,
            "reason": "invalid_verified_amount",
            "fallback_to_manual": False,
        }


    return {
        "ok": True,
        "amount": verified_amount,
        "sender_name": item.get("senderName"),
        "reference": (
            item.get("referenceNumber")
            or item.get("transactionNumber")
            or transaction_number
        ),
    }


# ============================================================
# TELEGRAM INIT DATA VALIDATION
# ============================================================

def validate_init_data(init_data: str):

    try:

        parsed = dict(
            urllib.parse.parse_qsl(
                init_data,
                strict_parsing=True
            )
        )

        received_hash = parsed.pop(
            "hash",
            None
        )

        if not received_hash:
            return None


        data_check_string = "\n".join(
            f"{k}={v}"
            for k, v in sorted(parsed.items())
        )


        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256,
        ).digest()


        computed_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()


        if not hmac.compare_digest(
            computed_hash,
            received_hash
        ):
            return None


        # ----------------------------------------------------
        # حماية من Replay Attack
        # ----------------------------------------------------

        auth_date = parsed.get("auth_date")

        if not auth_date:
            return None

        try:
            auth_timestamp = int(auth_date)
        except ValueError:
            return None

        # البيانات القديمة جدًا ترفض
        if abs(time.time() - auth_timestamp) > 86400:
            return None


        user = json.loads(
            parsed.get(
                "user",
                "{}"
            )
        )


        if "id" not in user:
            return None


        return user

    except Exception:

        return None


# ============================================================
# BINGO ENGINE
# ============================================================

COLUMN_RANGES = {
    "B": (1, 15),
    "I": (16, 30),
    "N": (31, 45),
    "G": (46, 60),
    "O": (61, 75),
}


def generate_grid():

    cols = {}

    for letter, (lo, hi) in COLUMN_RANGES.items():

        need = (
            4
            if letter == "N"
            else 5
        )

        cols[letter] = random.sample(
            range(lo, hi + 1),
            need
        )


    grid = []


    for row in range(5):

        line = []

        for letter in ["B", "I", "N", "G", "O"]:

            if (
                letter == "N"
                and row == 2
            ):

                line.append("FREE")

            else:

                idx = (
                    row - 1
                    if letter == "N" and row > 2
                    else row
                )

                line.append(
                    cols[letter][idx]
                )

        grid.append(line)


    return grid


def check_grid_win(grid, called_set):

    def marked(r, c):

        value = grid[r][c]

        return (
            value == "FREE"
            or value in called_set
        )


    # rows

    for r in range(5):

        if all(
            marked(r, c)
            for c in range(5)
        ):
            return True


    # columns

    for c in range(5):

        if all(
            marked(r, c)
            for r in range(5)
        ):
            return True


    # diagonal

    if all(
        marked(i, i)
        for i in range(5)
    ):
        return True


    # reverse diagonal

    if all(
        marked(i, 4 - i)
        for i in range(5)
    ):
        return True


    return False


# ============================================================
# ROUND MANAGEMENT
# ============================================================

def get_or_create_active_round():

    conn = db()

    row = conn.execute(
        """
        SELECT *
        FROM rounds
        WHERE status IN ('collecting', 'active')
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()


    if row is None:

        close_at = (
            time.time()
            + COLLECT_SECONDS
        )


        conn.execute(
            """
            INSERT INTO rounds(
                status,
                close_at,
                called_balls,
                prize_pool,
                created_at
            )
            VALUES (
                'collecting',
                ?,
                '[]',
                0,
                ?
            )
            """,
            (
                close_at,
                datetime.utcnow().isoformat(),
            )
        )


        conn.commit()


        row = conn.execute(
            """
            SELECT *
            FROM rounds
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


    conn.close()

    return row


# ============================================================
# ROUND ENGINE TICK
# ============================================================

def round_engine_tick():

    with _db_lock:

        conn = db()

        rnd = conn.execute(
            """
            SELECT *
            FROM rounds
            WHERE status IN ('collecting', 'active')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


        now = time.time()


        if rnd is None:

            conn.close()

            get_or_create_active_round()

            return


        # ====================================================
        # 1. CLOSE COLLECTION
        # ====================================================

        if (
            rnd["status"] == "collecting"
            and now >= rnd["close_at"]
        ):

            sequence = list(
                range(1, 76)
            )

            random.shuffle(sequence)


            conn.execute(
                """
                UPDATE rounds
                SET
                    status='active',
                    ball_sequence=?,
                    active_started_at=?,
                    next_ball_at=?
                WHERE id=?
                """,
                (
                    json.dumps(sequence),
                    now,
                    now,
                    rnd["id"],
                )
            )


            conn.commit()

            conn.close()

            log.info(
                "Round %s started. Prize pool: %s ETB",
                rnd["id"],
                rnd["prize_pool"],
            )

            return


        # ====================================================
        # 2. DRAW NEXT BALL
        # ====================================================

        if (
            rnd["status"] == "active"
            and now >= rnd["next_ball_at"]
        ):

            sequence = json.loads(
                rnd["ball_sequence"]
            )

            called = json.loads(
                rnd["called_balls"]
            )


            if len(called) >= 75:

                conn.execute(
                    """
                    UPDATE rounds
                    SET
                        status='finished',
                        finished_at=?
                    WHERE id=?
                    """,
                    (
                        now,
                        rnd["id"],
                    )
                )

                conn.commit()

                conn.close()

                get_or_create_active_round()

                return


            next_ball = sequence[
                len(called)
            ]

            called.append(next_ball)

            called_set = set(called)


            log.info(
                "Round %s ball: %s",
                rnd["id"],
                next_ball,
            )


            entries = conn.execute(
                """
                SELECT *
                FROM round_entries
                WHERE round_id=?
                """,
                (rnd["id"],)
            ).fetchall()


            winners = []


            for entry in entries:

                grid = json.loads(
                    entry["grid"]
                )

                if check_grid_win(
                    grid,
                    called_set
                ):

                    winners.append(entry)


            # =================================================
            # FIRST BINGO
            # =================================================

            if winners:

                pool = int(
                    rnd["prize_pool"]
                )


                share = (
                    pool // len(winners)
                    if winners
                    else 0
                )


                log.info(
                    "Round %s FINISHED. "
                    "Ball=%s Winners=%s Pool=%s Share=%s",
                    rnd["id"],
                    next_ball,
                    len(winners),
                    pool,
                    share,
                )


                for winner in winners:

                    conn.execute(
                        """
                        UPDATE round_entries
                        SET
                            is_winner=1,
                            payout=?
                        WHERE id=?
                        """,
                        (
                            share,
                            winner["id"],
                        )
                    )


                    conn.execute(
                        """
                        UPDATE users
                        SET balance = balance + ?
                        WHERE user_id=?
                        """,
                        (
                            share,
                            winner["user_id"],
                        )
                    )


                conn.execute(
                    """
                    UPDATE rounds
                    SET
                        called_balls=?,
                        status='finished',
                        finished_at=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(called),
                        now,
                        rnd["id"],
                    )
                )


                conn.commit()

                conn.close()


                # الجولة التالية
                get_or_create_active_round()

                return


            # =================================================
            # NO WINNER YET
            # =================================================

            if len(called) >= 75:

                conn.execute(
                    """
                    UPDATE rounds
                    SET
                        called_balls=?,
                        status='finished',
                        finished_at=?
                    WHERE id=?
                    """,
                    (
                        json.dumps(called),
                        now,
                        rnd["id"],
                    )
                )


                conn.commit()

                conn.close()

                get_or_create_active_round()

                return


            # =================================================
            # CONTINUE
            # =================================================

            conn.execute(
                """
                UPDATE rounds
                SET
                    called_balls=?,
                    next_ball_at=?
                WHERE id=?
                """,
                (
                    json.dumps(called),
                    now + BALL_DRAW_SECONDS,
                    rnd["id"],
                )
            )


            conn.commit()

            conn.close()

            return


        conn.close()


# ============================================================
# ROUND LOOP
# ============================================================

def round_engine_loop():

    log.info(
        "Royal Bingo Round Engine started."
    )


    while True:

        try:

            round_engine_tick()

        except Exception as e:

            log.exception(
                "round_engine_tick error: %s",
                e
            )


        time.sleep(1)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.config[
    "MAX_CONTENT_LENGTH"
] = 8 * 1024 * 1024


# ============================================================
# RAW TELEGRAM BOT
# ============================================================

_raw_bot = Bot(
    token=BOT_TOKEN
)


def send_admin_message_sync(
    text,
    reply_markup=None
):

    return asyncio.run(
        _raw_bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            reply_markup=reply_markup,
        )
    )


def send_user_message_sync(
    user_id,
    text
):

    try:

        asyncio.run(
            _raw_bot.send_message(
                chat_id=user_id,
                text=text,
            )
        )

    except Exception as e:

        log.warning(
            "send_user_message_sync failed: %s",
            e
        )


# ============================================================
# CORS
# ============================================================

@app.after_request
def add_cors(resp):

    resp.headers[
        "Access-Control-Allow-Origin"
    ] = "*"

    resp.headers[
        "Access-Control-Allow-Headers"
    ] = "Content-Type"

    resp.headers[
        "Access-Control-Allow-Methods"
    ] = "GET,POST,OPTIONS"

    return resp


@app.route(
    "/api/<path:_any>",
    methods=["OPTIONS"]
)
def cors_preflight(_any):

    return ("", 204)


# ============================================================
# AUTH
# ============================================================

def auth_or_error(payload):

    user = validate_init_data(
        payload.get(
            "initData",
            ""
        )
    )


    if user is None:

        return None, (
            jsonify(
                {
                    "error":
                    "invalid_init_data"
                }
            ),
            401,
        )


    return user, None


# ============================================================
# API AUTH
# ============================================================

@app.route(
    "/api/auth",
    methods=["POST"]
)
def api_auth():

    payload = request.get_json(
        force=True
    )

    user, err = auth_or_error(
        payload
    )

    if err:
        return err


    balance = get_or_create_user(
        user["id"],
        user.get("username")
    )


    return jsonify(
        {
            "user_id": user["id"],
            "username": user.get(
                "username"
            ),
            "balance": balance,
        }
    )


# ============================================================
# PUBLIC ROUND STATE
# ============================================================

def round_to_public_dict(
    rnd,
    entries_for_user=None
):

    now = time.time()


    out = {
        "round_id": rnd["id"],
        "status": rnd["status"],
        "prize_pool": rnd["prize_pool"],
        "called_balls": json.loads(
            rnd["called_balls"]
        ),
    }


    if rnd["status"] == "collecting":

        out["closes_in"] = max(
            0,
            round(
                rnd["close_at"] - now
            )
        )


    if entries_for_user is not None:

        out["your_entries"] = [
            {
                "grid": json.loads(
                    e["grid"]
                ),
                "is_winner": bool(
                    e["is_winner"]
                ),
                "payout": e["payout"],
            }
            for e in entries_for_user
        ]


    return out


# ============================================================
# CURRENT ROUND
# ============================================================

@app.route(
    "/api/round/current",
    methods=["POST"]
)
def api_round_current():

    payload = request.get_json(
        force=True
    )

    user, err = auth_or_error(
        payload
    )

    if err:
        return err


    rnd = get_or_create_active_round()


    conn = db()

    entries = conn.execute(
        """
        SELECT *
        FROM round_entries
        WHERE round_id=?
          AND user_id=?
        """,
        (
            rnd["id"],
            user["id"],
        )
    ).fetchall()


    conn.close()


    return jsonify(
        round_to_public_dict(
            rnd,
            entries
        )
    )


# ============================================================
# JOIN ROUND
# ============================================================

@app.route(
    "/api/round/join",
    methods=["POST"]
)
def api_round_join():

    payload = request.get_json(
        force=True
    )

    user, err = auth_or_error(
        payload
    )

    if err:
        return err


    try:

        count = int(
            payload.get(
                "count",
                1
            )
        )

    except (TypeError, ValueError):

        return jsonify(
            {
                "error":
                "invalid_count"
            }
        ), 400


    if count < 1:

        return jsonify(
            {
                "error":
                "invalid_count"
            }
        ), 400


    if count > MAX_CARTELAS:

        return jsonify(
            {
                "error":
                "max_cartelas_exceeded",
                "max":
                MAX_CARTELAS,
            }
        ), 400


    with _db_lock:

        conn = db()


        rnd = conn.execute(
            """
            SELECT *
            FROM rounds
            WHERE status='collecting'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()


        if rnd is None:

            conn.close()

            return jsonify(
                {
                    "error":
                    "no_open_round"
                }
            ), 400


        # ----------------------------------------------------
        # تأكد أن نافذة الانضمام لم تنته فعليًا
        # ----------------------------------------------------

        if time.time() >= rnd["close_at"]:

            conn.close()

            return jsonify(
                {
                    "error":
                    "round_closing"
                }
            ), 400


        existing = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM round_entries
            WHERE round_id=?
              AND user_id=?
            """,
            (
                rnd["id"],
                user["id"],
            )
        ).fetchone()["c"]


        if existing + count > MAX_CARTELAS:

            conn.close()

            return jsonify(
                {
                    "error":
                    "max_cartelas_exceeded",
                    "max":
                    MAX_CARTELAS,
                    "already":
                    existing,
                }
            ), 400


        cost = (
            ENTRY_STAKE * count
        )


        bal_row = conn.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (user["id"],)
        ).fetchone()


        balance = (
            bal_row["balance"]
            if bal_row
            else 0
        )


        if balance < cost:

            conn.close()

            return jsonify(
                {
                    "error":
                    "insufficient_balance",
                    "balance":
                    balance,
                    "needed":
                    cost,
                }
            ), 400


        # ----------------------------------------------------
        # الخصم
        # ----------------------------------------------------

        conn.execute(
            """
            UPDATE users
            SET balance = balance - ?
            WHERE user_id=?
            """,
            (
                cost,
                user["id"],
            )
        )


        # ----------------------------------------------------
        # 80% Prize Pool
        # ----------------------------------------------------

        pool_add = int(
            cost * PAYOUT_RATIO
        )


        conn.execute(
            """
            UPDATE rounds
            SET prize_pool =
                prize_pool + ?
            WHERE id=?
            """,
            (
                pool_add,
                rnd["id"],
            )
        )


        new_grids = []


        for _ in range(count):

            grid = generate_grid()


            conn.execute(
                """
                INSERT INTO round_entries(
                    round_id,
                    user_id,
                    username,
                    grid,
                    stake_paid
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    rnd["id"],
                    user["id"],
                    user.get(
                        "username"
                    ),
                    json.dumps(grid),
                    ENTRY_STAKE,
                )
            )


            new_grids.append(grid)


        conn.commit()


        new_balance = (
            balance - cost
        )


        conn.close()


    return jsonify(
        {
            "ok": True,
            "grids": new_grids,
            "balance": new_balance,
            "round_id": rnd["id"],
        }
    )


# ============================================================
# ROUND STATE
# ============================================================

@app.route(
    "/api/round/state",
    methods=["POST"]
)
def api_round_state():

    payload = request.get_json(
        force=True
    )

    user, err = auth_or_error(
        payload
    )

    if err:
        return err


    try:

        round_id = int(
            payload.get(
                "round_id"
            )
        )

    except (TypeError, ValueError):

        return jsonify(
            {
                "error":
                "invalid_round_id"
            }
        ), 400


    conn = db()


    rnd = conn.execute(
        """
        SELECT *
        FROM rounds
        WHERE id=?
        """,
        (round_id,)
    ).fetchone()


    if rnd is None:

        conn.close()

        return jsonify(
            {
                "error":
                "round_not_found"
            }
        ), 404


    entries = conn.execute(
        """
        SELECT *
        FROM round_entries
        WHERE round_id=?
          AND user_id=?
        """,
        (
            round_id,
            user["id"],
        )
    ).fetchall()


    balance_row = conn.execute(
        """
        SELECT balance
        FROM users
        WHERE user_id=?
        """,
        (user["id"],)
    ).fetchone()


    balance = (
        balance_row["balance"]
        if balance_row
        else 0
    )


    conn.close()


    result = round_to_public_dict(
        rnd,
        entries
    )


    result["balance"] = balance


    return jsonify(result)


# ============================================================
# DEPOSIT API
# ============================================================

@app.route(
    "/api/deposit/submit",
    methods=["POST"]
)
def api_deposit_submit():

    payload = request.get_json(
        force=True
    )

    user, err = auth_or_error(
        payload
    )

    if err:
        return err


    txn_id = str(
        payload.get(
            "txn_id",
            ""
        )
    ).strip()


    if not txn_id:

        return jsonify(
            {
                "error":
                "missing_txn_id"
            }
        ), 400


    try:

        claimed_amount = int(
            payload.get(
                "amount",
                0
            )
        )

    except (TypeError, ValueError):

        claimed_amount = 0


    # --------------------------------------------------------
    # تحقق محلي أولًا
    # --------------------------------------------------------

    with _db_lock:

        conn = db()

        existing = conn.execute(
            """
            SELECT *
            FROM deposits
            WHERE txn_id=?
            LIMIT 1
            """,
            (txn_id,)
        ).fetchone()

        conn.close()


    if existing:

        if existing["status"] == "completed":

            return jsonify(
                {
                    "error":
                    "duplicate_transaction"
                }
            ), 400

        return jsonify(
            {
                "error":
                "transaction_already_submitted",
                "status":
                existing["status"],
            }
        ), 400


    # --------------------------------------------------------
    # Verify.ET
    # --------------------------------------------------------

    result = verify_telebirr_payment(
        txn_id
    )


    # ========================================================
    # VERIFIED
    # ========================================================

    if result["ok"]:

        verified_amount = int(
            result["amount"]
        )


        with _db_lock:

            conn = db()

            # فحص نهائي ضد race condition
            existing = conn.execute(
                """
                SELECT id
                FROM deposits
                WHERE txn_id=?
                LIMIT 1
                """,
                (txn_id,)
            ).fetchone()


            if existing:

                conn.close()

                return jsonify(
                    {
                        "error":
                        "duplicate_transaction"
                    }
                ), 400


            cur = conn.execute(
                """
                INSERT INTO deposits(
                    user_id,
                    username,
                    amount,
                    method,
                    txn_id,
                    photo_file_id,
                    status,
                    created_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, 'completed', ?
                )
                """,
                (
                    user["id"],
                    user.get(
                        "username",
                        ""
                    ),
                    verified_amount,
                    DEPOSIT_METHOD,
                    txn_id,
                    None,
                    datetime.utcnow().isoformat(),
                )
            )


            dep_id = cur.lastrowid


            conn.execute(
                """
                UPDATE users
                SET balance =
                    balance + ?
                WHERE user_id=?
                """,
                (
                    verified_amount,
                    user["id"],
                )
            )


            balance_row = conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (user["id"],)
            ).fetchone()


            new_balance = (
                balance_row["balance"]
                if balance_row
                else 0
            )


            conn.commit()

            conn.close()


        send_admin_message_sync(
            f"✅ Auto-Verified Deposit #{dep_id}\n\n"
            f"Player: @{user.get('username') or user['id']} "
            f"(ID: {user['id']})\n"
            f"Amount: {verified_amount} ETB\n"
            f"Sender: {result.get('sender_name', '?')}\n"
            f"Txn: {txn_id}\n\n"
            f"Balance: {new_balance} ETB"
        )


        return jsonify(
            {
                "ok": True,
                "status":
                "completed",
                "credited_amount":
                verified_amount,
                "balance":
                new_balance,
            }
        )


    # ========================================================
    # HARD REJECT
    # ========================================================

    if not result.get(
        "fallback_to_manual"
    ):

        return jsonify(
            {
                "error":
                result["reason"]
            }
        ), 400


    # ========================================================
    # MANUAL FALLBACK
    # ========================================================

    with _db_lock:

        conn = db()


        existing = conn.execute(
            """
            SELECT id
            FROM deposits
            WHERE txn_id=?
            LIMIT 1
            """,
            (txn_id,)
        ).fetchone()


        if existing:

            conn.close()

            return jsonify(
                {
                    "error":
                    "transaction_already_submitted"
                }
            ), 400


        cur = conn.execute(
            """
            INSERT INTO deposits(
                user_id,
                username,
                amount,
                method,
                txn_id,
                photo_file_id,
                status,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, 'pending', ?
            )
            """,
            (
                user["id"],
                user.get(
                    "username",
                    ""
                ),
                claimed_amount,
                DEPOSIT_METHOD,
                txn_id,
                None,
                datetime.utcnow().isoformat(),
            )
        )


        dep_id = cur.lastrowid

        conn.commit()

        conn.close()


    caption = (
        f"⚠️ Deposit #{dep_id}\n\n"
        f"Auto verification unavailable: "
        f"{result['reason']}\n\n"
        f"Player: @{user.get('username') or user['id']} "
        f"(ID: {user['id']})\n"
        f"Claimed Amount: {claimed_amount} ETB\n"
        f"Method: {DEPOSIT_METHOD}\n"
        f"Transaction ID: {txn_id}\n\n"
        f"Status: pending"
    )


    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ ቀበል",
                callback_data=f"dep_ok_{dep_id}"
            ),
            InlineKeyboardButton(
                "✕ ውድቅ",
                callback_data=f"dep_no_{dep_id}"
            ),
        ]]
    )


    try:

        msg = send_admin_message_sync(
            caption,
            kb
        )


        conn = db()

        conn.execute(
            """
            UPDATE deposits
            SET
                admin_msg_chat_id=?,
                admin_msg_id=?
            WHERE id=?
            """,
            (
                msg.chat_id,
                msg.message_id,
                dep_id,
            )
        )


        conn.commit()

        conn.close()


    except Exception as e:

        log.exception(
            "Failed to notify admin of deposit #%s: %s",
            dep_id,
            e
        )


    return jsonify(
        {
            "ok": True,
            "status": "pending",
            "deposit_id": dep_id,
        }
    )


# ============================================================
# WITHDRAW API
# ============================================================

@app.route(
    "/api/withdraw/submit",
    methods=["POST"]
)
def api_withdraw_submit():

    payload = request.get_json(
        force=True
    )

    user, err = auth_or_error(
        payload
    )

    if err:
        return err


    try:

        amount = int(
            payload.get(
                "amount",
                0
            )
        )

    except (TypeError, ValueError):

        return jsonify(
            {
                "error":
                "invalid_amount"
            }
        ), 400


    if amount <= 0:

        return jsonify(
            {
                "error":
                "invalid_amount"
            }
        ), 400


    account = str(
        payload.get(
            "account",
            ""
        )
    ).strip()


    if not account:

        return jsonify(
            {
                "error":
                "missing_account"
            }
        ), 400


    with _db_lock:

        conn = db()


        balance_row = conn.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (user["id"],)
        ).fetchone()


        current_balance = (
            balance_row["balance"]
            if balance_row
            else 0
        )


        if amount > current_balance:

            conn.close()

            return jsonify(
                {
                    "error":
                    "insufficient_balance",
                    "balance":
                    current_balance,
                }
            ), 400


        cur = conn.execute(
            """
            INSERT INTO withdrawals(
                user_id,
                username,
                amount,
                method,
                account,
                status,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, 'pending', ?
            )
            """,
            (
                user["id"],
                user.get(
                    "username",
                    ""
                ),
                amount,
                DEPOSIT_METHOD,
                account,
                datetime.utcnow().isoformat(),
            )
        )


        wd_id = cur.lastrowid


        conn.commit()

        conn.close()


    text = (
        f"💸 Withdrawal #{wd_id}\n\n"
        f"Player: @{user.get('username') or user['id']} "
        f"(ID: {user['id']})\n"
        f"Amount: {amount} ETB\n"
        f"Method: {DEPOSIT_METHOD}\n"
        f"Account: {account}\n"
        f"Current Balance: {current_balance} ETB\n\n"
        f"Status: pending"
    )


    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ ተከፍሏል",
                callback_data=f"wd_ok_{wd_id}"
            ),
            InlineKeyboardButton(
                "✕ ውድቅ",
                callback_data=f"wd_no_{wd_id}"
            ),
        ]]
    )


    try:

        msg = send_admin_message_sync(
            text,
            kb
        )


        conn = db()

        conn.execute(
            """
            UPDATE withdrawals
            SET
                admin_msg_chat_id=?,
                admin_msg_id=?
            WHERE id=?
            """,
            (
                msg.chat_id,
                msg.message_id,
                wd_id,
            )
        )


        conn.commit()

        conn.close()


    except Exception as e:

        log.exception(
            "Failed to notify admin of withdrawal #%s: %s",
            wd_id,
            e
        )


    return jsonify(
        {
            "ok": True,
            "withdrawal_id": wd_id,
        }
    )


# ============================================================
# TELEGRAM BOT
# ============================================================

BTN_PLAY = "🎰 ጨዋታ ጀምር"

BTN_DEPOSIT = "💰 ገንዘብ ማስገባት"

BTN_WITHDRAW = "💸 ገንዘብ ማውጣት"

BTN_BALANCE = "💳 ቀሪ ሂሳብ"

BTN_SUPPORT = "🆘 ድጋፍ"


def main_menu():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    BTN_PLAY,
                    web_app=WebAppInfo(
                        url=WEBAPP_URL
                    )
                )
            ],
            [
                BTN_DEPOSIT,
                BTN_WITHDRAW
            ],
            [
                BTN_BALANCE,
                BTN_SUPPORT
            ],
        ],
        resize_keyboard=True,
    )


def cancel_kb():

    return ReplyKeyboardMarkup(
        [["/cancel"]],
        resize_keyboard=True
    )


def is_admin(uid: int):

    return uid in ADMIN_IDS


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user


    get_or_create_user(
        user.id,
        user.username
    )


    await update.message.reply_text(
        "👑 ወደ ROYAL BINGO እንኳን በደህና መጡ!\n\n"
        "ከታች ካለው ሜኑ ይምረጡ፦",
        reply_markup=main_menu()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    bal = get_balance(
        update.effective_user.id
    )


    await update.message.reply_text(
        f"💳 ቀሪ ሂሳብዎ፦ {bal} ETB",
        reply_markup=main_menu()
    )


# ============================================================
# SUPPORT
# ============================================================

async def show_support(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        f"🆘 ለማንኛውም ጥያቄ እባክዎ ያግኙን፦ "
        f"{SUPPORT_CONTACT}",
        reply_markup=main_menu()
    )


# ============================================================
# DEPOSIT START
# ============================================================

async def deposit_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "ገንዘብ ለማስገባት ወደ ሚከተለው "
        "አካውንት ይላኩ፦\n\n"

        f"💳 ዘዴ፦ {DEPOSIT_METHOD}\n"
        f"👤 ስም፦ {DEPOSIT_ACCOUNT_NAME}\n"
        f"📱 ቁጥር፦ {DEPOSIT_ACCOUNT_NUMBER}\n\n"

        "ከላኩ በኋላ የላኩትን "
        "መጠን (በ ETB) ያስገቡ፦\n\n"

        "(ለመሰረዝ /cancel ይጫኑ)",

        reply_markup=cancel_kb()
    )


    return DEP_AMOUNT


# ============================================================
# DEPOSIT AMOUNT
# ============================================================

async def deposit_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()


    if (
        not text.isdigit()
        or int(text) <= 0
    ):

        await update.message.reply_text(
            "እባክዎ ትክክለኛ ቁጥር "
            "ያስገቡ (ምሳሌ፦ 100)፦"
        )

        return DEP_AMOUNT


    context.user_data[
        "dep_amount"
    ] = int(text)


    await update.message.reply_text(
        "እባክዎ የግብይት ቁጥር "
        "(Transaction ID) ያስገቡ፦"
    )


    return DEP_TXNID


# ============================================================
# DEPOSIT TXN ID
# ============================================================

async def deposit_txnid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    txn_id = update.message.text.strip()

    claimed_amount = context.user_data.get(
        "dep_amount",
        0
    )


    await update.message.reply_text(
        "⏳ በመጣራት ላይ... "
        "ትንሽ ይጠብቁ።"
    )


    # لا نجمد Telegram event loop
    result = await asyncio.to_thread(
        verify_telebirr_payment,
        txn_id
    )


    # ========================================================
    # VERIFIED
    # ========================================================

    if result["ok"]:

        verified_amount = int(
            result["amount"]
        )


        with _db_lock:

            conn = db()


            existing = conn.execute(
                """
                SELECT id
                FROM deposits
                WHERE txn_id=?
                LIMIT 1
                """,
                (txn_id,)
            ).fetchone()


            if existing:

                conn.close()

                await update.message.reply_text(
                    "❌ ይህ የግብይት ቁጥር "
                    "ቀድሞ ጥቅም ላይ ውሏል።",
                    reply_markup=main_menu()
                )

                context.user_data.clear()

                return ConversationHandler.END


            cur = conn.execute(
                """
                INSERT INTO deposits(
                    user_id,
                    username,
                    amount,
                    method,
                    txn_id,
                    photo_file_id,
                    status,
                    created_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, 'completed', ?
                )
                """,
                (
                    user.id,
                    user.username or "",
                    verified_amount,
                    DEPOSIT_METHOD,
                    txn_id,
                    None,
                    datetime.utcnow().isoformat(),
                )
            )


            dep_id = cur.lastrowid


            conn.execute(
                """
                UPDATE users
                SET balance =
                    balance + ?
                WHERE user_id=?
                """,
                (
                    verified_amount,
                    user.id,
                )
            )


            balance_row = conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (user.id,)
            ).fetchone()


            new_balance = (
                balance_row["balance"]
                if balance_row
                else 0
            )


            conn.commit()

            conn.close()


        await update.message.reply_text(
            f"🎉 ተረጋግጧል! "
            f"{verified_amount} ETB "
            f"ወደ ሂሳብዎ ገብቷል።\n"
            f"ቀሪ ሂሳብ፦ "
            f"{new_balance} ETB",
            reply_markup=main_menu()
        )


        try:

            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"✅ Auto-Verified Deposit #{dep_id}\n"
                f"Player: @{user.username or user.id} "
                f"(ID: {user.id})\n"
                f"Amount: {verified_amount} ETB\n"
                f"Sender: {result.get('sender_name', '?')}\n"
                f"Txn: {txn_id}\n"
                f"Balance: {new_balance} ETB"
            )

        except Exception as e:

            log.warning(
                "notify admin failed: %s",
                e
            )


        context.user_data.clear()

        return ConversationHandler.END


    # ========================================================
    # HARD REJECT
    # ========================================================

    if not result.get(
        "fallback_to_manual"
    ):

        reason_text = {

            "not_verified":
                "ይህ የግብይት ቁጥር "
                "ትክክል አይደለም።",

            "duplicate_transaction":
                "ይህ ግብይት ቀደም ብሎ "
                "ጥቅም ላይ ውሏል።",

            "wrong_recipient":
                "ይህ ክፍያ ወደ እኛ "
                "አካውንት አልደረሰም۔",

        }.get(
            result["reason"],
            "ማረጋገጫው አልተሳካም።"
        )


        await update.message.reply_text(
            f"❌ {reason_text}\n"
            f"እባክዎ ትክክለኛ መረጃ "
            f"በድጋሚ ያስገቡ ወይም "
            f"{SUPPORT_CONTACT} ያግኙ።",
            reply_markup=main_menu()
        )


        context.user_data.clear()

        return ConversationHandler.END


    # ========================================================
    # MANUAL FALLBACK
    # ========================================================

    with _db_lock:

        conn = db()


        existing = conn.execute(
            """
            SELECT id
            FROM deposits
            WHERE txn_id=?
            LIMIT 1
            """,
            (txn_id,)
        ).fetchone()


        if existing:

            conn.close()

            await update.message.reply_text(
                "❌ ይህ የግብይት ቁጥር "
                "ቀድሞ ቀርቧል።",
                reply_markup=main_menu()
            )

            context.user_data.clear()

            return ConversationHandler.END


        cur = conn.execute(
            """
            INSERT INTO deposits(
                user_id,
                username,
                amount,
                method,
                txn_id,
                photo_file_id,
                status,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, 'pending', ?
            )
            """,
            (
                user.id,
                user.username or "",
                claimed_amount,
                DEPOSIT_METHOD,
                txn_id,
                None,
                datetime.utcnow().isoformat(),
            )
        )


        dep_id = cur.lastrowid


        conn.commit()

        conn.close()


    caption = (
        f"⚠️ Deposit #{dep_id}\n\n"
        f"Auto verification unavailable: "
        f"{result['reason']}\n\n"
        f"Player: @{user.username or user.id} "
        f"(ID: {user.id})\n"
        f"Claimed Amount: {claimed_amount} ETB\n"
        f"Method: {DEPOSIT_METHOD}\n"
        f"Transaction ID: {txn_id}\n\n"
        f"Status: pending"
    )


    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ ቀበል",
                callback_data=f"dep_ok_{dep_id}"
            ),
            InlineKeyboardButton(
                "✕ ውድቅ",
                callback_data=f"dep_no_{dep_id}"
            ),
        ]]
    )


    msg = await context.bot.send_message(
        ADMIN_CHAT_ID,
        caption,
        reply_markup=kb
    )


    conn = db()

    conn.execute(
        """
        UPDATE deposits
        SET
            admin_msg_chat_id=?,
            admin_msg_id=?
        WHERE id=?
        """,
        (
            msg.chat_id,
            msg.message_id,
            dep_id,
        )
    )


    conn.commit()

    conn.close()


    await update.message.reply_text(
        f"✅ ጥያቄዎ (#{dep_id}) ደርሷል፣ "
        f"በእጅ በመታየት ላይ ነው።",
        reply_markup=main_menu()
    )


    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# WITHDRAW START
# ============================================================

async def withdraw_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    bal = get_balance(
        update.effective_user.id
    )


    if bal <= 0:

        await update.message.reply_text(
            "በቂ ቀሪ ሂሳብ የለዎትም።",
            reply_markup=main_menu()
        )

        return ConversationHandler.END


    await update.message.reply_text(
        f"ቀሪ ሂሳብዎ፦ {bal} ETB\n\n"
        "ምን ያህል ማውጣት ይፈልጋሉ? "
        "(በ ETB)፦\n\n"
        "(ለመሰረዝ /cancel ይጫኑ)",
        reply_markup=cancel_kb()
    )


    return WD_AMOUNT


# ============================================================
# WITHDRAW AMOUNT
# ============================================================

async def withdraw_amount(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text.strip()


    if (
        not text.isdigit()
        or int(text) <= 0
    ):

        await update.message.reply_text(
            "እባክዎ ትክክለኛ ቁጥር "
            "ያስገቡ፦"
        )

        return WD_AMOUNT


    amount = int(text)


    bal = get_balance(
        update.effective_user.id
    )


    if amount > bal:

        await update.message.reply_text(
            f"በቂ ቀሪ ሂሳብ የለዎትም። "
            f"ቀሪ ሂሳብዎ፦ {bal} ETB\n\n"
            "ሌላ መጠን ያስገቡ፦"
        )

        return WD_AMOUNT


    context.user_data[
        "wd_amount"
    ] = amount


    await update.message.reply_text(
        f"ገንዘቡ የሚላክበትን "
        f"የ{DEPOSIT_METHOD} ስልክ ቁጥር "
        f"ያስገቡ፦"
    )


    return WD_PHONE


# ============================================================
# WITHDRAW PHONE
# ============================================================

async def withdraw_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    account = update.message.text.strip()

    user = update.effective_user

    amount = context.user_data[
        "wd_amount"
    ]


    with _db_lock:

        conn = db()


        balance_row = conn.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (user.id,)
        ).fetchone()


        current_balance = (
            balance_row["balance"]
            if balance_row
            else 0
        )


        if amount > current_balance:

            conn.close()

            await update.message.reply_text(
                "❌ በቂ ቀሪ ሂሳብ የለዎትም።",
                reply_markup=main_menu()
            )

            context.user_data.clear()

            return ConversationHandler.END


        cur = conn.execute(
            """
            INSERT INTO withdrawals(
                user_id,
                username,
                amount,
                method,
                account,
                status,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, 'pending', ?
            )
            """,
            (
                user.id,
                user.username or "",
                amount,
                DEPOSIT_METHOD,
                account,
                datetime.utcnow().isoformat(),
            )
        )


        wd_id = cur.lastrowid


        conn.commit()

        conn.close()


    text = (
        f"💸 Withdrawal #{wd_id}\n\n"
        f"Player: @{user.username or user.id} "
        f"(ID: {user.id})\n"
        f"Amount: {amount} ETB\n"
        f"Method: {DEPOSIT_METHOD}\n"
        f"Account: {account}\n"
        f"Current Balance: {current_balance} ETB\n\n"
        f"Status: pending"
    )


    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "✅ ተከፍሏል",
                callback_data=f"wd_ok_{wd_id}"
            ),
            InlineKeyboardButton(
                "✕ ውድቅ",
                callback_data=f"wd_no_{wd_id}"
            ),
        ]]
    )


    msg = await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=text,
        reply_markup=kb
    )


    conn = db()

    conn.execute(
        """
        UPDATE withdrawals
        SET
            admin_msg_chat_id=?,
            admin_msg_id=?
        WHERE id=?
        """,
        (
            msg.chat_id,
            msg.message_id,
            wd_id,
        )
    )


    conn.commit()

    conn.close()


    await update.message.reply_text(
        f"✅ የማውጣት ጥያቄዎ (#{wd_id}) ደርሷል።\n"
        "አድሚን እስኪያረጋግጥ "
        "በመጠባበቅ ላይ ነው። "
        "ቀሪ ሂሳብዎ እስከዚያ ድረስ "
        "አይቀየርም።",
        reply_markup=main_menu()
    )


    context.user_data.clear()

    return ConversationHandler.END


# ============================================================
# CANCEL
# ============================================================

async def cancel_conv(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    context.user_data.clear()


    await update.message.reply_text(
        "ተሰርዟል።",
        reply_markup=main_menu()
    )


    return ConversationHandler.END


# ============================================================
# DEPOSIT ADMIN CALLBACK
# ============================================================

async def handle_deposit_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query


    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "⛔ You are not authorized.",
            show_alert=True
        )

        return


    parts = query.data.split("_")

    action = parts[1]

    dep_id = int(parts[2])

    approve = (
        action == "ok"
    )


    with _db_lock:

        conn = db()


        dep = conn.execute(
            """
            SELECT *
            FROM deposits
            WHERE id=?
              AND status='pending'
            """,
            (dep_id,)
        ).fetchone()


        if dep is None:

            conn.close()

            await query.answer(
                "⚠️ Already handled.",
                show_alert=True
            )

            return


        new_status = (
            "completed"
            if approve
            else "rejected"
        )


        conn.execute(
            """
            UPDATE deposits
            SET status=?
            WHERE id=?
            """,
            (
                new_status,
                dep_id,
            )
        )


        old_balance = get_balance(
            dep["user_id"]
        )


        if approve:

            conn.execute(
                """
                UPDATE users
                SET balance =
                    balance + ?
                WHERE user_id=?
                """,
                (
                    dep["amount"],
                    dep["user_id"],
                )
            )


        conn.commit()


        new_balance = get_balance(
            dep["user_id"]
        )


        conn.close()


    status_line = (
        "✅ ACCEPTED"
        if approve
        else
        "❌ REJECTED"
    )


    new_caption = (
        f"💰 Deposit #{dep_id}\n\n"
        f"Player: @{dep['username'] or dep['user_id']} "
        f"(ID: {dep['user_id']})\n"
        f"Amount: {dep['amount']} ETB\n"
        f"Method: {dep['method']}\n"
        f"Transaction ID: {dep['txn_id']}\n\n"
        f"Status: {status_line}"
    )


    if approve:

        new_caption += (
            f"\nBalance: "
            f"{old_balance} → "
            f"{new_balance} ETB"
        )


    await query.edit_message_text(
        text=new_caption
    )


    await query.answer(
        "Done ✅"
    )


    try:

        if approve:

            await context.bot.send_message(
                dep["user_id"],
                f"🎉 የክፍያ ጥያቄዎ "
                f"(#{dep_id}) ተቀባይነት አግኝቷል!\n"
                f"ቀሪ ሂሳብዎ፦ "
                f"{old_balance} → "
                f"{new_balance} ETB"
            )

        else:

            await context.bot.send_message(
                dep["user_id"],
                f"❌ የክፍያ ጥያቄዎ "
                f"(#{dep_id}) ውድቅ ተደርጓል።\n"
                f"ለበለጠ መረጃ "
                f"{SUPPORT_CONTACT} ያግኙ።"
            )


    except Exception as e:

        log.warning(
            "notify failed: %s",
            e
        )


# ============================================================
# WITHDRAW ADMIN CALLBACK
# ============================================================

async def handle_withdrawal_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query


    if not is_admin(
        query.from_user.id
    ):

        await query.answer(
            "⛔ You are not authorized.",
            show_alert=True
        )

        return


    parts = query.data.split("_")

    action = parts[1]

    wd_id = int(parts[2])

    approve = (
        action == "ok"
    )


    with _db_lock:

        conn = db()


        wd = conn.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE id=?
              AND status='pending'
            """,
            (wd_id,)
        ).fetchone()


        if wd is None:

            conn.close()

            await query.answer(
                "⚠️ Already handled.",
                show_alert=True
            )

            return


        if approve:

            bal_row = conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (wd["user_id"],)
            ).fetchone()


            bal = (
                bal_row["balance"]
                if bal_row
                else 0
            )


            if bal < wd["amount"]:

                conn.close()

                await query.answer(
                    f"⚠️ Insufficient balance! "
                    f"Current: {bal} ETB",
                    show_alert=True
                )

                return


            conn.execute(
                """
                UPDATE users
                SET balance =
                    balance - ?
                WHERE user_id=?
                """,
                (
                    wd["amount"],
                    wd["user_id"],
                )
            )


            conn.execute(
                """
                UPDATE withdrawals
                SET status='completed'
                WHERE id=?
                """,
                (wd_id,)
            )


            conn.commit()


            new_balance = get_balance(
                wd["user_id"]
            )


        else:

            conn.execute(
                """
                UPDATE withdrawals
                SET status='rejected'
                WHERE id=?
                """,
                (wd_id,)
            )


            conn.commit()

            new_balance = None


        conn.close()


    status_line = (
        "✅ PAID"
        if approve
        else
        "❌ REJECTED"
    )


    new_text = (
        f"💸 Withdrawal #{wd_id}\n\n"
        f"Player: @{wd['username'] or wd['user_id']} "
        f"(ID: {wd['user_id']})\n"
        f"Amount: {wd['amount']} ETB\n"
        f"Method: {wd['method']}\n"
        f"Account: {wd['account']}\n\n"
        f"Status: {status_line}"
    )


    if approve:

        new_text += (
            f"\nRemaining Balance: "
            f"{new_balance} ETB"
        )

    else:

        new_text += (
            "\nBalance unchanged"
        )


    await query.edit_message_text(
        text=new_text
    )


    await query.answer(
        "Done ✅"
    )


    try:

        if approve:

            await context.bot.send_message(
                wd["user_id"],
                f"✅ የማውጣት ጥያቄዎ "
                f"(#{wd_id}) ተልኳል!\n"
                f"የተላከ መጠን፦ "
                f"{wd['amount']} ETB\n"
                f"ቀሪ ሂሳብ፦ "
                f"{new_balance} ETB"
            )

        else:

            await context.bot.send_message(
                wd["user_id"],
                f"❌ የማውጣት ጥያቄዎ "
                f"(#{wd_id}) ውድቅ ተደርጓል።\n"
                f"ቀሪ ሂሳብዎ አልተቀየረም።"
            )


    except Exception as e:

        log.warning(
            "notify failed: %s",
            e
        )


# ============================================================
# BUILD BOT
# ============================================================

def build_bot_application():

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )


    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^{BTN_BALANCE}$"
            ),
            show_balance
        )
    )


    application.add_handler(
        MessageHandler(
            filters.Regex(
                f"^{BTN_SUPPORT}$"
            ),
            show_support
        )
    )


    # --------------------------------------------------------
    # Deposit
    # --------------------------------------------------------

    deposit_conv = ConversationHandler(

        entry_points=[
            MessageHandler(
                filters.Regex(
                    f"^{BTN_DEPOSIT}$"
                ),
                deposit_start
            )
        ],

        states={

            DEP_AMOUNT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    deposit_amount
                )
            ],

            DEP_TXNID: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    deposit_txnid
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_conv
            )
        ],
    )


    application.add_handler(
        deposit_conv
    )


    # --------------------------------------------------------
    # Withdrawal
    # --------------------------------------------------------

    withdraw_conv = ConversationHandler(

        entry_points=[
            MessageHandler(
                filters.Regex(
                    f"^{BTN_WITHDRAW}$"
                ),
                withdraw_start
            )
        ],

        states={

            WD_AMOUNT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    withdraw_amount
                )
            ],

            WD_PHONE: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    withdraw_phone
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_conv
            )
        ],
    )


    application.add_handler(
        withdraw_conv
    )


    application.add_handler(
        CallbackQueryHandler(
            handle_deposit_callback,
            pattern=r"^dep_"
        )
    )


    application.add_handler(
        CallbackQueryHandler(
            handle_withdrawal_callback,
            pattern=r"^wd_"
        )


    )


    return application


# ============================================================
# BOT THREAD
# ============================================================

def run_bot_in_thread():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(loop)


    application = (
        build_bot_application()
    )


    log.info(
        "Telegram bot polling starting..."
    )


    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    init_db()


    get_or_create_active_round()


    threading.Thread(
        target=run_bot_in_thread,
        daemon=True,
        name="telegram-bot"
    ).start()


    threading.Thread(
        target=round_engine_loop,
        daemon=True,
        name="round-engine"
    ).start()


    log.info(
        "Flask API starting on port %s ...",
        PORT
    )


    app.run(
        host="0.0.0.0",
        port=PORT,
        threaded=True
    )