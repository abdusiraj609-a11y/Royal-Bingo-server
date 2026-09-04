# -*- coding: utf-8 -*-
import os
import sqlite3
import requests
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ========== الإعدادات ==========
BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
VERIFY_ET_API_KEY = os.environ.get('VERIFY_ET_API_KEY', '')
VERIFY_ET_BASE_URL = "https://verify.et"
TELEBIRR_SETTLEMENT_ACCOUNT = os.environ.get('TELEBIRR_SETTLEMENT_ACCOUNT', '')
DB_NAME = "bingo.db"

def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def verify_telebirr_transaction(transaction_id):
    url = f"{VERIFY_ET_BASE_URL}/api/verify?waitMs=5000"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": VERIFY_ET_API_KEY,
        "Idempotency-Key": f"verify-{uuid.uuid4()}"
    }
    payload = {"bank": "telebirr", "transactionNumber": transaction_id}
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
            return True, None
        return False, data.get("message", "Transaction not verified")
    except Exception as e:
        return False, str(e)

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
            await query.edit_message_text(f"የእርስዎ ቀሪ ሂሳብ: {player['balance']:.2f} ETB")
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

        await update.message.reply_text(f"✅ ገንዘቡ በተሳካ ሁኔታ ገብቷል! አዲስ ቀሪ ሂሳብ: {player['balance'] + amount:.2f} ETB")
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

def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN not set")
        return
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("deposit", deposit_command))
    application.add_handler(CommandHandler("withdraw", withdraw_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.run_polling()

if __name__ == '__main__':
    main()
