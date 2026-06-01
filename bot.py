import logging
import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import librouteros
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 🔧 কমেন্ট নাম (পুরো নেটওয়ার্ক জ্যামের জন্য)
JAM_RULE_COMMENT = "facebook-mbps"

active_timers = {}
application = None

def connect_to_mikrotik():
    try:
        return librouteros.connect(
            host=os.getenv("ROUTER_IP"),
            username=os.getenv("ROUTER_USER"),
            password=os.getenv("ROUTER_PASS"),
            port=int(os.getenv("ROUTER_PORT", 8728))
        )
    except Exception as e:
        logging.error(f"MikroTik connect failed: {e}")
        return None

def jam_network():
    api = connect_to_mikrotik()
    if api:
        try:
            # আগে চেক করি অলরেডি জ্যাম আছে কি না
            rules = api.path('ip', 'firewall', 'filter')
            for rule in rules:
                if rule.get('comment') == JAM_RULE_COMMENT:
                    return True
            # পুরো নেটওয়ার্ক জ্যাম রুল
            api.path('ip', 'firewall', 'filter').add(
                chain='forward', action='drop', comment=JAM_RULE_COMMENT
            )
            return True
        finally:
            api.close()
    return False

def unjam_network():
    api = connect_to_mikrotik()
    if api:
        try:
            rules = api.path('ip', 'firewall', 'filter')
            for rule in rules:
                if rule.get('comment') == JAM_RULE_COMMENT:
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
            return True
        finally:
            api.close()
    return False

async def auto_unjam(chat_id, seconds):
    await asyncio.sleep(seconds)
    if unjam_network():
        await application.bot.send_message(chat_id=chat_id, text="✅ সময় শেষ! নেটওয়ার্ক আনজ্যাম হয়েছে।")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id

    if query.data == "jam":
        await query.edit_message_text("⏱️ কতক্ষণের জন্য জ্যাম করবেন?\n(যেমন: 10s, 20s, 1m)")
        context.user_data['waiting_for_time'] = True
    elif query.data == "unjam":
        if unjam_network():
            await query.edit_message_text("✅ নেটওয়ার্ক আনজ্যাম করা হয়েছে।")
        else:
            await query.edit_message_text("⚠️ নেটওয়ার্ক ইতিমধ্যে সচল আছে।")
    elif query.data == "status":
        api = connect_to_mikrotik()
        if api:
            rules = api.path('ip', 'firewall', 'filter')
            is_jammed = any(r.get('comment') == JAM_RULE_COMMENT for r in rules)
            status = "❌ জ্যাম অবস্থায় আছে" if is_jammed else "✅ সচল আছে"
            await query.edit_message_text(f"📊 বর্তমান অবস্থা: {status}")
            api.close()

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('waiting_for_time'):
        text = update.message.text.lower()
        seconds = 0
        if 's' in text: seconds = int(text.replace('s', ''))
        elif 'm' in text: seconds = int(text.replace('m', '')) * 60
        
        if seconds > 0:
            if jam_network():
                await update.message.reply_text(f"🔒 নেটওয়ার্ক জ্যাম করা হয়েছে {seconds} সেকেন্ডের জন্য।")
                asyncio.create_task(auto_unjam(update.effective_chat.id, seconds))
            context.user_data['waiting_for_time'] = False

def main():
    global application
    application = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("মেনু:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 জ্যাম", callback_data="jam"), InlineKeyboardButton("🟢 আনজ্যাম", callback_data="unjam")],
        [InlineKeyboardButton("📊 স্ট্যাটাস", callback_data="status")]
    ]))))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT, handle_time))
    application.run_polling()

if __name__ == '__main__':
    main()