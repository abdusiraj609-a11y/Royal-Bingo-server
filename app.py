# -*- coding: utf-8 -*-
import os
import sqlite3
import requests
import uuid
import threading
import asyncio
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

app = Flask(__name__)
CORS(app)

# ========== الإعدادات ==========
DB_NAME = "bingo.db"
CONFIG_ENTRY_STAKE = 10
CONFIG_PAYOUT_RATIO = 0.80
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', 'change-this-secret-token')
VERIFY_ET_API_KEY = os.environ.get('VERIFY_ET_API_KEY', '')
VERIFY_ET_BASE_URL = "https://verify.et"
TELEBIRR_SETTLEMENT_ACCOUNT = os.environ.get('TELEBIRR_SETTLEMENT_ACCOUNT', '')
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')

# ========== قاعدة البيانات ==========
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS players (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id TEXT UNIQUE NOT NULL,
        username TEXT,
        balance REAL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        type TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT DEFAULT 'completed',
        reference TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (player_id) REFERENCES players(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS withdrawal_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id INTEGER NOT NULL,
        amount REAL NOT NULL,
        method TEXT NOT NULL,
        phone_number TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        admin_note TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME,
        FOREIGN KEY (player_id) REFERENCES players(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS rounds (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_number INTEGER UNIQUE NOT NULL,
        start_time DATETIME,
        close_time DATETIME,
        status TEXT DEFAULT 'open',
        prize_pool REAL DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS cartelas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER,
        player_id INTEGER,
        slot_number INTEGER,
        data TEXT,
        is_winner INTEGER DEFAULT 0,
        purchased_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (round_id) REFERENCES rounds(id),
        FOREIGN KEY (player_id) REFERENCES players(id),
        UNIQUE (round_id, slot_number)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS drawn_balls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER,
        ball_number INTEGER,
        drawn_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (round_id) REFERENCES rounds(id),
        UNIQUE (round_id, ball_number)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS winners (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        round_id INTEGER,
        player_id INTEGER,
        cartela_id INTEGER,
        prize_amount REAL,
        win_time DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (round_id) REFERENCES rounds(id),
        FOREIGN KEY (player_id) REFERENCES players(id),
        FOREIGN KEY (cartela_id) REFERENCES cartelas(id)
    )''')

    c.execute('INSERT OR IGNORE INTO rounds (id, round_number, status) VALUES (1, 1, "open")')
    conn.commit()
    conn.close()

init_db()

# ========== دوال مساعدة ==========
def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def get_player_balance(player_id):
    conn = get_db()
    row = conn.execute('SELECT balance FROM players WHERE id = ?', (player_id,)).fetchone()
    conn.close()
    return row['balance'] if row else 0

def add_transaction(player_id, trans_type, amount, status='completed', reference=None):
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO transactions (player_id, type, amount, status, reference) VALUES (?,?,?,?,?)',
        (player_id, trans_type, amount, status, reference)
    )
    conn.commit()
    tx_id = cur.lastrowid
    conn.close()
    return tx_id

def require_admin(req):
    token = req.headers.get('X-Admin-Token')
    return token == ADMIN_TOKEN

def verify_telebirr_transaction(transaction_id):
    url = f"{VERIFY_ET_BASE_URL}/api/verify?waitMs=5000"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": VERIFY_ET_API_KEY,
        "Idempotency-Key": f"verify-{uuid.uuid4()}"
    }
    payload = {
        "bank": "telebirr",
        "transactionNumber": transaction_id
    }
    if TELEBIRR_SETTLEMENT_ACCOUNT:
        payload["settlementAccount"] = TELEBIRR_SETTLEMENT_ACCOUNT

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        data = response.json()
        if response.status_code != 200:
            return False, data.get("message", "Verification request failed")
        verification = data.get("verification", {})
        if verification.get("verified") or (data.get("success") and data.get("data")):
            transactions = data.get("data", [])
            if transactions:
                amount = transactions[0].get("amount")
                return True, amount
            else:
                return True, None
        else:
            return False, data.get("message", "Transaction not verified")
    except Exception as e:
        return False, str(e)

# ========== نقاط API ==========
@app.route('/')
def home():
    return jsonify({'message': 'የሮያል ቢንጎ አገልጋይ እየሰራ ነው'})

@app.route('/api/test')
def test():
    return jsonify({'status': 'OK'})

@app.route('/api/players', methods=['POST'])
def create_player():
    data = request.json
    telegram_id = data.get('telegram_id')
    username = data.get('username')
    if not telegram_id:
        return jsonify({'error': 'telegram_id ያስፈልጋል'}), 400

    conn = get_db()
    row = conn.execute('SELECT * FROM players WHERE telegram_id = ?', (telegram_id,)).fetchone()
    if row:
        conn.close()
        return jsonify(dict(row))

    cur = conn.execute('INSERT INTO players (telegram_id, username) VALUES (?, ?)', (telegram_id, username))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({'id': new_id, 'telegram_id': telegram_id, 'username': username, 'balance': 0}), 201

@app.route('/api/balance/<int:player_id>')
def get_balance(player_id):
    return jsonify({'balance': get_player_balance(player_id)})

@app.route('/api/deposits', methods=['POST'])
def deposit():
    data = request.json
    player_id = data.get('player_id')
    amount = data.get('amount')
    transaction_id = data.get('transaction_id')

    if not player_id or not amount or amount <= 0:
        return jsonify({'error': 'የተሳሳተ የተጫዋች መለያ ወይም መጠን'}), 400
    if not transaction_id:
        return jsonify({'error': 'የግብይት ቁጥር ያስፈልጋል'}), 400

    is_valid, verified_amount = verify_telebirr_transaction(transaction_id)
    if not is_valid:
        return jsonify({'error': 'ግብይቱ አልተረጋገጠም'}), 400
    if verified_amount is not None and float(verified_amount) < amount:
        return jsonify({'error': 'የገንዘብ መጠኑ ከተረጋገጠው ጋር አይዛመድም'}), 400

    conn = get_db()
    conn.execute('UPDATE players SET balance = balance + ? WHERE id = ?', (amount, player_id))
    conn.commit()
    conn.close()

    tx_id = add_transaction(player_id, 'deposit', amount, 'completed', transaction_id)
    return jsonify({'success': True, 'transactionId': tx_id, 'message': 'ገንዘቡ በተሳካ ሁኔታ ገብቷል'})

@app.route('/api/withdrawals', methods=['POST'])
def withdraw():
    data = request.json
    player_id = data.get('player_id')
    amount = data.get('amount')
    method = data.get('method')
    phone_number = data.get('phone_number')
    if not player_id or not amount or amount <= 0 or not method or not phone_number:
        return jsonify({'error': 'አስፈላጊ መስኮች ጎድለዋል'}), 400

    conn = get_db()
    player = conn.execute('SELECT balance FROM players WHERE id = ?', (player_id,)).fetchone()
    if not player or player['balance'] < amount:
        conn.close()
        return jsonify({'error': 'በቂ ቀሪ ሂሳብ የለም'}), 400

    conn.execute('UPDATE players SET balance = balance - ? WHERE id = ?', (amount, player_id))
    cur = conn.execute(
        'INSERT INTO withdrawal_requests (player_id, amount, method, phone_number) VALUES (?,?,?,?)',
        (player_id, amount, method, phone_number)
    )
    conn.commit()
    request_id = cur.lastrowid
    conn.close()

    add_transaction(player_id, 'withdrawal', -amount, 'pending', 'የመውጣት ጥያቄ')
    return jsonify({'id': request_id, 'status': 'pending'}), 201

@app.route('/api/transactions/<int:player_id>')
def get_transactions(player_id):
    conn = get_db()
    rows = conn.execute('SELECT * FROM transactions WHERE player_id = ? ORDER BY created_at DESC LIMIT 50', (player_id,)).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/cartelas', methods=['POST'])
def buy_cartela():
    data = request.json
    player_id = data.get('player_id')
    round_id = data.get('round_id', 1)
    slot_number = data.get('slot_number')
    cost = CONFIG_ENTRY_STAKE

    if not player_id or not slot_number:
        return jsonify({'error': 'የጎደለ መረጃ'}), 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM cartelas WHERE round_id=? AND slot_number=?', (round_id, slot_number)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'ስሎቱ አስቀድሞ ተይዟል'}), 409

    player = conn.execute('SELECT balance FROM players WHERE id=?', (player_id,)).fetchone()
    if not player or player['balance'] < cost:
        conn.close()
        return jsonify({'error': 'በቂ ቀሪ ሂሳብ የለም'}), 400

    conn.execute('UPDATE players SET balance = balance - ? WHERE id=?', (cost, player_id))
    conn.execute('INSERT INTO cartelas (round_id, player_id, slot_number) VALUES (?,?,?)', (round_id, player_id, slot_number))
    conn.execute('INSERT INTO transactions (player_id, type, amount, status, reference) VALUES (?,?,?,?,?)',
                 (player_id, 'purchase', -cost, 'completed', f'slot:{slot_number}'))
    conn.execute('UPDATE rounds SET prize_pool = prize_pool + ? WHERE id=?', (cost * CONFIG_PAYOUT_RATIO, round_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/rounds/current')
def current_round():
    conn = get_db()
    row = conn.execute("SELECT * FROM rounds WHERE status='open' ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if row:
        return jsonify(dict(row))
    return jsonify({'error': 'ምንም ክፍት ዙር የለም'}), 404

@app.route('/api/admin/withdrawals/<int:request_id>/approve', methods=['POST'])
def approve_withdrawal(request_id):
    if not require_admin(request):
        return jsonify({'error': 'ያልተፈቀደ መዳረሻ'}), 401
    conn = get_db()
    req = conn.execute('SELECT * FROM withdrawal_requests WHERE id = ?', (request_id,)).fetchone()
    if not req:
        conn.close()
        return jsonify({'error': 'ጥያቄው አልተገኘም'}), 404
    if req['status'] != 'pending':
        conn.close()
        return jsonify({'error': 'ጥያቄው አስቀድሞ ተስተናግዷል'}), 400

    conn.execute("UPDATE withdrawal_requests SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (request_id,))
    conn.execute("UPDATE transactions SET status = 'completed' WHERE player_id = ? AND type = 'withdrawal' AND status = 'pending' AND amount = ? ORDER BY id DESC LIMIT 1",
                 (req['player_id'], -req['amount']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/api/admin/withdrawals/<int:request_id>/reject', methods=['POST'])
def reject_withdrawal(request_id):
    if not require_admin(request):
        return jsonify({'error': 'ያልተፈቀደ መዳረሻ'}), 401
    conn = get_db()
    req = conn.execute('SELECT * FROM withdrawal_requests WHERE id = ?', (request_id,)).fetchone()
    if not req:
        conn.close()
        return jsonify({'error': 'ጥያቄው አልተገኘም'}), 404
    if req['status'] != 'pending':
        conn.close()
        return jsonify({'error': 'ጥያቄው አስቀድሞ ተስተናግዷል'}), 400

    conn.execute('UPDATE players SET balance = balance + ? WHERE id = ?', (req['amount'], req['player_id']))
    conn.execute("UPDATE withdrawal_requests SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (request_id,))
    conn.execute("UPDATE transactions SET status = 'rejected' WHERE player_id = ? AND type = 'withdrawal' AND status = 'pending' AND amount = ? ORDER BY id DESC LIMIT 1",
                 (req['player_id'], -req['amount']))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ========== بوت تيليغرام ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💰 Balance", callback_data='balance')],
        [InlineKeyboardButton("💸 Deposit", callback_data='deposit'),
         InlineKeyboardButton("💵 Withdraw", callback_data='withdraw')],
        [InlineKeyboardButton("🔗 Referral", callback_data='referral'),
         InlineKeyboardButton("🆘 Support", callback_data='support')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("እንኳን ደህና መጡ! አንዱን ይምረጡ፡", reply_markup=reply_markup)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == 'balance':
        telegram_id = str(query.from_user.id)
        conn = get_db()
        player = conn.execute('SELECT * FROM players WHERE telegram_id = ?', (telegram_id,)).fetchone()
        conn.close()
        if player:
            balance = player['balance']
            await query.edit_message_text(f"የእርስዎ ቀሪ ሂሳብ: {balance:.2f} ETB")
        else:
            await query.edit_message_text("አሁንም ተጫዋች አይደሉም። በመጀመሪያ ጨዋታውን ይክፈቱ።")

    elif data == 'deposit':
        settlement = TELEBIRR_SETTLEMENT_ACCOUNT or 'N/A'
        await query.edit_message_text(
            f"💸 ገንዘብ ለማስገባት ወደዚህ Telebirr ሂሳብ ይላኩ:\n\n"
            f"📞 {settlement}\n\n"
            f"ከዚያም በሚከተለው ቅርጸት የግብይት ቁጥሩን ይላኩ:\n"
            f"/deposit <amount> <transaction_id>\n\n"
            f"ለምሳሌ: /deposit 100 DET8FJGUJ4"
        )

    elif data == 'withdraw':
        await query.edit_message_text(
            "እባክዎ የመውጣት ጥያቄውን በሚከተለው ቅርጸት ይላኩ:\n\n"
            "/withdraw <amount> <phone_number>\n\n"
            "ለምሳሌ: /withdraw 50 0911223344"
        )

    elif data == 'referral':
        await query.edit_message_text("የማጣቀሻ ሊንክዎን ለማግኘት በቅርቡ ይገኛል።")

    elif data == 'support':
        await query.edit_message_text("ለእገዛ እባክዎ @YourSupportUsername ያነጋግሩ።")

async def deposit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("ትክክለኛ ቅርጸት: /deposit <amount> <transaction_id>")
            return
        amount = float(args[0])
        transaction_id = args[1]
        telegram_id = str(update.effective_user.id)

        conn = get_db()
        player = conn.execute('SELECT * FROM players WHERE telegram_id = ?', (telegram_id,)).fetchone()
        conn.close()
        if not player:
            await update.message.reply_text("በመጀመሪያ ጨዋታውን ይክፈቱ።")
            return

        is_valid, verified_amount = verify_telebirr_transaction(transaction_id)
        if not is_valid:
            await update.message.reply_text("ግብይቱ አልተረጋገጠም።")
            return
        if verified_amount is not None and float(verified_amount) < amount:
            await update.message.reply_text("የገንዘብ መጠኑ ከተረጋገጠው ጋር አይዛመድም።")
            return

        conn = get_db()
        conn.execute('UPDATE players SET balance = balance + ? WHERE id = ?', (amount, player['id']))
        conn.execute('INSERT INTO transactions (player_id, type, amount, status, reference) VALUES (?,?,?,?,?)',
                     (player['id'], 'deposit', amount, 'completed', transaction_id))
        conn.commit()
        conn.close()

        await update.message.reply_text(f"✅ ገንዘቡ በተሳካ ሁኔታ ገብቷል! አዲስ ቀሪ ሂሳብ: {get_player_balance(player['id']):.2f} ETB")
    except Exception as e:
        await update.message.reply_text(f"ስህተት: {str(e)}")

async def withdraw_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        args = context.args
        if len(args) < 2:
            await update.message.reply_text("ትክክለኛ ቅርጸት: /withdraw <amount> <phone_number>")
            return
        amount = float(args[0])
        phone = args[1]
        telegram_id = str(update.effective_user.id)

        conn = get_db()
        player = conn.execute('SELECT * FROM players WHERE telegram_id = ?', (telegram_id,)).fetchone()
        if not player:
            conn.close()
            await update.message.reply_text("ተጫዋች አልተገኘም።")
            return
        if player['balance'] < amount:
            conn.close()
            await update.message.reply_text("በቂ ቀሪ ሂሳብ የለም።")
            return

        conn.execute('UPDATE players SET balance = balance - ? WHERE id = ?', (amount, player['id']))
        conn.execute('INSERT INTO withdrawal_requests (player_id, amount, method, phone_number) VALUES (?,?,?,?)',
                     (player['id'], amount, 'Telebirr', phone))
        conn.execute('INSERT INTO transactions (player_id, type, amount, status, reference) VALUES (?,?,?,?,?)',
                     (player['id'], 'withdrawal', -amount, 'pending', 'Withdrawal request'))
        conn.commit()
        conn.close()

        await update.message.reply_text("✅ የመውጣት ጥያቄዎ ተልኳል። በቅርቡ ይገመገማል።")
    except Exception as e:
        await update.message.reply_text(f"ስህተት: {str(e)}")

# إعداد البوت والـ webhook
bot_app = None
if BOT_TOKEN:
    bot_app = Application.builder().token(BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("deposit", deposit_command))
    bot_app.add_handler(CommandHandler("withdraw", withdraw_command))
    bot_app.add_handler(CallbackQueryHandler(button_handler))
else:
    print("BOT_TOKEN not set — Telegram bot will not start.")

@app.route('/telegram-webhook', methods=['POST'])
async def telegram_webhook():
    if bot_app is None:
        return jsonify({'error': 'Bot not configured'}), 500
    update = Update.de_json(request.get_json(force=True), bot_app.bot)
    await bot_app.process_update(update)
    return jsonify({'status': 'ok'})

def set_webhook():
    if bot_app is None:
        return
    webhook_url = f"{os.environ.get('RENDER_EXTERNAL_URL', 'https://royal-bingo-server.onrender.com')}/telegram-webhook"
    asyncio.run(bot_app.bot.set_webhook(webhook_url))

# بدء البوت وضبط webhook
if bot_app is not None:
    threading.Thread(target=set_webhook, daemon=True).start()
