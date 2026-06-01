import logging
import os
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import librouteros
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# 🔧 কমেন্ট নাম
JAM_NET_COMMENT = "facebook-mbps"
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

# --- জ্যাম লজিক (অটোমেটিক) ---
def jam_network():
    api = connect_to_mikrotik()
    if api:
        try:
            api.path('ip', 'firewall', 'filter').add(chain='forward', action='drop', comment=JAM_NET_COMMENT)
            return True
        finally: api.close()
    return False

def jam_user_by_ip(username):
    api = connect_to_mikrotik()
    if api:
        try:
            # পিপিটিপি/পিপিপিই থেকে আইপি খুঁজে বের করা
            active_users = api.path('ppp', 'active')
            for user in active_users:
                if user.get('name') == username:
                    ip = user.get('address')
                    api.path('ip', 'firewall', 'filter').add(chain='forward', action='drop', src_address=ip, comment=f"jam-{username}")
                    return True
            return False
        finally: api.close()
    return False

def unjam_all(username=None):
    api = connect_to_mikrotik()
    if api:
        try:
            rules = api.path('ip', 'firewall', 'filter')
            for rule in rules:
                if username and rule.get('comment') == f"jam-{username}":
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
                elif not username and rule.get('comment') == JAM_NET_COMMENT:
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
            return True
        finally: api.close()
    return False

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "jam":
        await query.edit_message_text("⏱️ পুরো নেট কতক্ষণ জ্যাম করবেন? (যেমন: 10s, 1m)")
        context.user_data['action'] = 'jam_net'
        context.user_data['waiting_for_input'] = True
    elif query.data == "jam_user":
        await query.edit_message_text("👤 ইউজারনেম এবং সময় দিন (যেমন: 28001 1m)")
        context.user_data['action'] = 'jam_user'
        context.user_data['waiting_for_input'] = True
    elif query.data == "unjam":
        unjam_all()
        await query.edit_message_text("✅ নেটওয়ার্ক আনজ্যাম হয়েছে।")

async def handle_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('waiting_for_input'):
        text = update.message.text.lower().split()
        action = context.user_data.get('action')
        
        # সময় নির্ধারণ
        time_str = text[1] if action == 'jam_user' else text[0]
        sec = int(time_str.replace('s', '').replace('m', '')) * (60 if 'm' in time_str else 1)
        
        if action == 'jam_net':
            if jam_network():
                await update.message.reply_text(f"🔒 নেট জ্যাম হয়েছে {sec} সেকেন্ডের জন্য।")
                await asyncio.sleep(sec)
                unjam_all()
                await update.message.reply_text("✅ নেটওয়ার্ক আনজ্যাম হয়েছে।")
        
        elif action == 'jam_user':
            user = text[0]
            if jam_user_by_ip(user):
                await update.message.reply_text(f"👤 ইউজার {user} জ্যাম হয়েছে।")
await asyncio.sleep(sec)
                unjam_all(user)
                await update.message.reply_text(f"✅ ইউজার {user} আনজ্যাম হয়েছে।")
            else:
                await update.message.reply_text("⚠️ ইউজার অনলাইন নেই!")
        
        context.user_data['waiting_for_input'] = False

def main():
    global application
    application = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    application.add_handler(CommandHandler("start", lambda u, c: u.message.reply_text("মেনু:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 নেট জ্যাম", callback_data="jam"), InlineKeyboardButton("👤 ইউজার জ্যাম", callback_data="jam_user")],
        [InlineKeyboardButton("🟢 আনজ্যাম", callback_data="unjam")]
    ]))))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT, handle_time))
    application.run_polling()

if name == 'main':
    main()
