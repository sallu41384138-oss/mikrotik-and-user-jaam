import logging
import os
import time
import asyncio
import threading
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import librouteros
from dotenv import load_dotenv
from flask import Flask, request, Response

load_dotenv()
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

JAM_RULE_COMMENT = "facebook-mbps"
USER_JAM_COMMENT = "Mbps speed"
ADDRESS_LIST_NAME = "Mbps Speed"
BOT_PIN = os.getenv("BOT_PIN")
if not BOT_PIN:
    logging.warning("⚠️ BOT_PIN .env এ সেট করা নেই! PIN verify কাজ করবে না। .env এ BOT_PIN=1234 এর মতো যোগ করুন।")
authorized_chats = set()  # যেসব chat_id সঠিক PIN দিয়ে verify হয়েছে
active_jams = {}
# ইউজার জ্যাম অবস্থায় ডিসকানেক্ট হলে এখানে রাখা হয় (net আবার active হলে অটো re-jam করার জন্য)
disconnected_jams = {}
FULL_JAM_KEY = "__full_jam__"
bot_app_instance = None

# ─── Flask ────────────────────────────────────────────────────────────────────
app_flask = Flask(__name__)

@app_flask.route('/', methods=['GET'])
def home():
    return "Bot is alive and running!", 200

@app_flask.route('/update_cred', methods=['GET'])
def webhook_update_all_credentials():
    try:
        new_ip   = request.args.get('ip')
        new_user = request.args.get('user')
        new_pass = request.args.get('pass')
        if not new_ip or not new_user or not new_pass:
            return Response("Missing ip, user or pass", status=400)
        if os.path.exists(".env"):
            with open(".env", "r") as f:
                lines = f.readlines()
            with open(".env", "w") as f:
                for line in lines:
                    if line.startswith("ROUTER_IP="):
                        f.write(f"ROUTER_IP={new_ip}\n")
                    elif line.startswith("ROUTER_USER="):
                        f.write(f"ROUTER_USER={new_user}\n")
                    elif line.startswith("ROUTER_PASS="):
                        f.write(f"ROUTER_PASS={new_pass}\n")
                    else:
                        f.write(line)
        os.environ["ROUTER_IP"]   = new_ip
        os.environ["ROUTER_USER"] = new_user
        os.environ["ROUTER_PASS"] = new_pass
        logging.info(f"🔄 Router Config Auto-Synced! IP: {new_ip} | User: {new_user}")
        return Response("Success", status=200)
    except Exception as e:
        return Response(str(e), status=500)

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app_flask.run(host='0.0.0.0', port=port)


# ─── MikroTik ─────────────────────────────────────────────────────────────────
def connect():
    return librouteros.connect(
        host=os.getenv("ROUTER_IP"),
        username=os.getenv("ROUTER_USER"),
        password=os.getenv("ROUTER_PASS"),
        port=int(os.getenv("ROUTER_PORT", 8728))
    )


# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_time(text):
    text = text.strip().lower()
    try:
        if text.endswith('h'):
            return int(text[:-1]) * 3600
        elif text.endswith('m'):
            return int(text[:-1]) * 60
        elif text.endswith('s'):
            return int(text[:-1])
        else:
            return int(text)
    except (ValueError, IndexError):
        return None


def format_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def safe_send_kwargs(text):
    """
    Markdown parse_mode ব্যবহার না করে plain text পাঠানো —
    ইউজারনাম/পাসওয়ার্ডে _ * ` থাকলে Markdown crash করে।
    """
    return {"text": text}


def add_filter_rule_on_top(api, **kwargs):
    """
    ip/firewall/filter এ নতুন রুল যোগ করে, কিন্তু ডিফল্ট append-at-bottom না করে
    লিস্টের সবার উপরে (place-before) বসায়। কারণ RouterOS রুল top-to-bottom
    চেক করে — নিচে থাকলে আগের কোনো accept রুল ট্র্যাফিক আগেই পাস করিয়ে দিতে পারে,
    ফলে drop রুল কখনো hit-ই হয় না এবং জ্যাম বাস্তবে কাজ করে না।
    """
    try:
        existing = list(api.path('ip', 'firewall', 'filter'))
    except Exception:
        existing = []
    if existing:
        kwargs['place-before'] = existing[0]['.id']
    api.path('ip', 'firewall', 'filter').add(**kwargs)


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 নেট জ্যাম",    callback_data="jam"),
         InlineKeyboardButton("🟢 নেট আনজ্যাম",  callback_data="unjam")],
        [InlineKeyboardButton("📊 স্ট্যাটাস",    callback_data="status"),
         InlineKeyboardButton("🔍 ক্রেডেনশিয়াল", callback_data="show_cred")],
        [InlineKeyboardButton("👤 ইউজার জ্যাম",  callback_data="jam_user"),
         InlineKeyboardButton("🔑 পাসওয়ার্ড",   callback_data="pass_lookup")],
    ])


# ─── Startup cleanup ──────────────────────────────────────────────────────────
def clear_old_rules():
    try:
        logging.info("Clearing old jam rules from MikroTik on startup...")
        api = connect()

        rules = list(api.path('ip', 'firewall', 'filter'))
        for rule in rules:
            if rule.get('comment') in (USER_JAM_COMMENT, JAM_RULE_COMMENT):
                try:
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
                except Exception:
                    pass

        entries = list(api.path('ip', 'firewall', 'address-list'))
        for entry in entries:
            if entry.get('comment') == USER_JAM_COMMENT or entry.get('list') == ADDRESS_LIST_NAME:
                try:
                    api.path('ip', 'firewall', 'address-list').remove(entry['.id'])
                except Exception:
                    pass

        api.close()
        logging.info("✅ Old rules cleared on startup!")
    except Exception as e:
        logging.error(f"Startup cleanup error: {e}")


# ─── /start ───────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in authorized_chats:
        await update.message.reply_text(
            "🖥️ মাইক্রোটিক বট মেনু:",
            reply_markup=main_menu_keyboard()
        )
        return

    context.user_data['step'] = 'waiting_for_pin'
    await update.message.reply_text("🔒 চালিয়ে যেতে আপনার PIN লিখুন:")


# ─── Callback handler ─────────────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if update.effective_chat.id not in authorized_chats:
        await query.answer("🔒 আগে /start চাপুন এবং PIN verify করুন।", show_alert=True)
        return

    await query.answer()

    # ── ইউজার জ্যাম শুরু ──
    if query.data == "jam_user":
        context.user_data['step'] = 'waiting_for_ip'
        await query.edit_message_text("👤 PPPoE ইউজার নাম লিখুন:")

    # ── নির্দিষ্ট IP আনজ্যাম ──
    elif query.data.startswith("unjam_"):
        target_ip = query.data[len("unjam_"):]
        try:
            api = connect()
            for entry in list(api.path('ip', 'firewall', 'address-list')):
                if entry.get('address') == target_ip and entry.get('comment') == USER_JAM_COMMENT:
                    api.path('ip', 'firewall', 'address-list').remove(entry['.id'])

            remaining = [
                e for e in api.path('ip', 'firewall', 'address-list')
                if e.get('list') == ADDRESS_LIST_NAME and e.get('comment') == USER_JAM_COMMENT
            ]
            if not remaining:
                for rule in list(api.path('ip', 'firewall', 'filter')):
                    if rule.get('src-address-list') == ADDRESS_LIST_NAME and rule.get('comment') == USER_JAM_COMMENT:
                        api.path('ip', 'firewall', 'filter').remove(rule['.id'])
            api.close()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return

        # মেমোরি থেকে টাস্ক বাতিল (active + disconnected উভয় জায়গা থেকে)
        for k in [k for k, v in active_jams.items()
                  if isinstance(v, dict) and v.get('current_ip') == target_ip]:
            active_jams[k]['task'].cancel()
            del active_jams[k]
        for k in [k for k, v in disconnected_jams.items()
                  if isinstance(v, dict) and v.get('old_ip') == target_ip]:
            del disconnected_jams[k]

        await query.edit_message_text(f"✅ আনজ্যাম সফল!\nIP: {target_ip}")

    # ── পুরো নেট জ্যাম শুরু ──
    elif query.data == "jam":
        context.user_data['waiting_for_time'] = True
        await query.edit_message_text(
            "⏱️ কতক্ষণের জন্য নেট জ্যাম করবেন?\n"
            "(যেমন: 30s, 10m, 1h)"
        )

    # ── পুরো নেট আনজ্যাম / জ্যাম বন্ধ ──
    elif query.data in ("unjam", "stop_full_jam"):
        if FULL_JAM_KEY in active_jams:
            active_jams[FULL_JAM_KEY]['task'].cancel()
            del active_jams[FULL_JAM_KEY]
        try:
            api = connect()
            for rule in list(api.path('ip', 'firewall', 'filter')):
                if rule.get('comment') == JAM_RULE_COMMENT:
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
            api.close()
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
            return
        await query.edit_message_text("✅ নেটওয়ার্ক সচল করা হয়েছে।")

    # ── স্ট্যাটাস ──
    elif query.data == "status":
        try:
            api = connect()
            is_jammed = any(
                r.get('comment') == JAM_RULE_COMMENT
                for r in api.path('ip', 'firewall', 'filter')
            )
            api.close()
            status = "❌ পুরো নেট জ্যাম" if is_jammed else "✅ সচল"

            # active_jams মেমোরি থেকেই আসল ইউজারনেম-ভিত্তিক লিস্ট বানানো হচ্ছে
            jammed_users = {
                k: v for k, v in active_jams.items() if k != FULL_JAM_KEY
            }

            text = f"📊 নেটওয়ার্ক: {status}"

            if jammed_users:
                text += f"\n\n👤 জ্যামড ইউজার ({len(jammed_users)} জন):"
                for uid, info in jammed_users.items():
                    text += f"\n• {uid} — IP: {info.get('current_ip', 'N/A')}"

            if disconnected_jams:
                text += f"\n\n⏸️ ডিসকানেক্টেড (জ্যাম পেন্ডিং) ({len(disconnected_jams)} জন):"
                for uid, info in disconnected_jams.items():
                    text += f"\n• {uid} — পুরোনো IP: {info.get('old_ip', 'N/A')}"

            kb_rows = []
            for uid, info in jammed_users.items():
                ip = info.get('current_ip')
                if ip:
                    kb_rows.append([InlineKeyboardButton(f"🚫 আনজ্যাম: {uid}", callback_data=f"unjam_{ip}")])
            for uid, info in disconnected_jams.items():
                ip = info.get('old_ip')
                if ip:
                    kb_rows.append([InlineKeyboardButton(f"🚫 আনজ্যাম: {uid}", callback_data=f"unjam_{ip}")])

            reply_markup = InlineKeyboardMarkup(kb_rows) if kb_rows else None
            await query.edit_message_text(text, reply_markup=reply_markup)
        except Exception as e:
            await query.edit_message_text(f"❌ রাউটার কানেক্ট হয়নি: {e}")

    # ── ক্রেডেনশিয়াল ──  (plain text — Markdown parse error এড়াতে)
    elif query.data == "show_cred":
        r_ip   = os.getenv("ROUTER_IP",   "Not Set")
        r_user = os.getenv("ROUTER_USER", "Not Set")
        r_pass = os.getenv("ROUTER_PASS", "Not Set")
        text = (
            "⚙️ বর্তমান রাউটার তথ্য:\n"
            "━━━━━━━━━━━━━━━━━━━\n"
            f"🌐 IP Address : {r_ip}\n"
            f"👤 Username   : {r_user}\n"
            f"🔑 Password   : {r_pass}\n"
            "━━━━━━━━━━━━━━━━━━━"
        )
        back_kb = [[InlineKeyboardButton("⬅️ প্রধান মেনু", callback_data="back_to_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(back_kb))

    # ── পাসওয়ার্ড দেখা/পরিবর্তন শুরু ──
    elif query.data == "pass_lookup":
        context.user_data['step'] = 'pass_waiting_for_user'
        await query.edit_message_text("🔑 PPPoE ইউজার নাম লিখুন (পাসওয়ার্ড দেখতে বা পরিবর্তন করতে):")

    # ── পাসওয়ার্ড পরিবর্তন করার অনুরোধ ──
    elif query.data.startswith("change_pass_"):
        target_user = query.data[len("change_pass_"):]
        context.user_data['step']        = 'pass_waiting_for_new'
        context.user_data['pass_target'] = target_user
        await query.edit_message_text(
            f"🔑 {target_user} এর নতুন পাসওয়ার্ড লিখুন:"
        )

    # ── প্রোফাইল পরিবর্তন করার অনুরোধ ──
    elif query.data.startswith("change_profile_"):
        target_user = query.data[len("change_profile_"):]
        context.user_data['step']           = 'profile_waiting_for_new'
        context.user_data['profile_target'] = target_user
        # রাউটার থেকে বিদ্যমান প্রোফাইল লিস্ট এনে বাটন হিসেবে দেখাও
        try:
            api = connect()
            profiles = [p.get('name') for p in api.path('ppp', 'profile') if p.get('name')]
            api.close()
        except Exception as e:
            await query.edit_message_text(f"❌ রাউটার কানেক্ট হয়নি: {e}")
            return

        if profiles:
            # প্রতিটা প্রোফাইল একটা বাটন হিসেবে দেখাও
            kb = [[InlineKeyboardButton(p, callback_data=f"set_profile_{target_user}||{p}")]
                  for p in profiles if p != 'default']
            kb.append([InlineKeyboardButton("✍️ নিজে লিখব", callback_data=f"type_profile_{target_user}")])
            await query.edit_message_text(
                f"📋 {target_user} এর নতুন প্রোফাইল বেছে নিন:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
        else:
            await query.edit_message_text(f"📋 {target_user} এর নতুন প্রোফাইল নাম লিখুন:")

    # ── প্রোফাইল বাটন থেকে সরাসরি সেট ──
    elif query.data.startswith("set_profile_"):
        payload     = query.data[len("set_profile_"):]
        target_user, new_profile = payload.split("||", 1)
        try:
            api = connect()
            secrets = list(api.path('ppp', 'secret'))
            secret_id = next((s['.id'] for s in secrets
                              if str(s.get('name', '')).strip() == target_user), None)
            if not secret_id:
                await query.edit_message_text(f"❌ '{target_user}' পাওয়া যায়নি।")
                api.close()
                return
            api.path('ppp', 'secret').update(**{'.id': secret_id, 'profile': new_profile})
            api.close()
        except Exception as e:
            await query.edit_message_text(f"❌ প্রোফাইল পরিবর্তন ব্যর্থ: {e}")
            return

        context.user_data['step']           = None
        context.user_data['profile_target'] = None
        await query.edit_message_text(
            f"✅ প্রোফাইল সফলভাবে পরিবর্তন হয়েছে!\n"
            f"👤 ইউজার       : {target_user}\n"
            f"📋 নতুন প্রোফাইল: {new_profile}"
        )

    # ── নিজে প্রোফাইল টাইপ করার অনুরোধ ──
    elif query.data.startswith("type_profile_"):
        target_user = query.data[len("type_profile_"):]
        context.user_data['step']           = 'profile_waiting_for_new'
        context.user_data['profile_target'] = target_user
        await query.edit_message_text(f"📋 {target_user} এর নতুন প্রোফাইল নাম লিখুন:")

    # ── প্রধান মেনু ──
    elif query.data == "back_to_menu":
        await query.edit_message_text(
            "🖥️ মাইক্রোটিক বট মেনু:",
            reply_markup=main_menu_keyboard()
        )


# ─── Full jam task ────────────────────────────────────────────────────────────
async def perform_full_jam(sec, update):
    try:
        api = connect()
        add_filter_rule_on_top(
            api,
            chain='forward', action='drop', comment=JAM_RULE_COMMENT
        )
        api.close()

        stop_kb = [[InlineKeyboardButton("🟢 জ্যাম বন্ধ করুন", callback_data="stop_full_jam")]]
        msg = await update.message.reply_text(
            f"🔴 সম্পূর্ণ নেটওয়ার্ক জ্যাম হয়েছে!\n"
            f"⏱️ সময়: {sec} সেকেন্ড\n\n"
            "নিচের বাটন দিয়ে যেকোনো সময় বন্ধ করুন:",
            reply_markup=InlineKeyboardMarkup(stop_kb)
        )

        await asyncio.sleep(sec)

        # সময় শেষে রুল সরাও
        api = connect()
        for rule in list(api.path('ip', 'firewall', 'filter')):
            if rule.get('comment') == JAM_RULE_COMMENT:
                try:
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
                except Exception:
                    pass
        api.close()

        active_jams.pop(FULL_JAM_KEY, None)
        await msg.reply_text("✅ সময় শেষ — নেটওয়ার্ক আবার সচল হয়েছে।")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"perform_full_jam error: {e}")
        try:
            await update.message.reply_text(f"❌ জ্যাম Error: {e}")
        except Exception:
            pass


# ─── User jam task ────────────────────────────────────────────────────────────
async def perform_jam(target_ip, target_id, sec, chat_id, app_bot):
    try:
        api = connect()
        api.path('ip', 'firewall', 'address-list').add(
            list=ADDRESS_LIST_NAME, address=target_ip, comment=USER_JAM_COMMENT
        )
        rule_exists = any(
            r.get('src-address-list') == ADDRESS_LIST_NAME and r.get('comment') == USER_JAM_COMMENT
            for r in api.path('ip', 'firewall', 'filter')
        )
        if not rule_exists:
            add_filter_rule_on_top(
                api,
                chain='forward', action='drop',
                **{'src-address-list': ADDRESS_LIST_NAME},
                comment=USER_JAM_COMMENT
            )
        api.close()

        kb = [[InlineKeyboardButton("🚫 জ্যাম বন্ধ করুন", callback_data=f"unjam_{target_ip}")]]
        await app_bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔴 জ্যাম সফল!\n"
                f"👤 ইউজার: {target_id}\n"
                f"🌐 IP: {target_ip}\n"
                f"⏱️ সময়: {sec} সেকেন্ড"
            ),
            reply_markup=InlineKeyboardMarkup(kb)
        )

        await asyncio.sleep(sec)

        # সময় শেষে address-list পরিষ্কার
        api = connect()
        for entry in list(api.path('ip', 'firewall', 'address-list')):
            if entry.get('address') == target_ip and entry.get('comment') == USER_JAM_COMMENT:
                api.path('ip', 'firewall', 'address-list').remove(entry['.id'])

        remaining = [
            e for e in api.path('ip', 'firewall', 'address-list')
            if e.get('list') == ADDRESS_LIST_NAME and e.get('comment') == USER_JAM_COMMENT
        ]
        if not remaining:
            for rule in list(api.path('ip', 'firewall', 'filter')):
                if rule.get('src-address-list') == ADDRESS_LIST_NAME and rule.get('comment') == USER_JAM_COMMENT:
                    api.path('ip', 'firewall', 'filter').remove(rule['.id'])
        api.close()

        active_jams.pop(target_id, None)
        await app_bot.send_message(
            chat_id=chat_id,
            text=f"✅ {target_id} — সময় শেষ, আনজ্যাম হয়েছে।"
        )

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logging.error(f"perform_jam error ({target_id}): {e}")
        # ব্যর্থ হলে চ্যাটে জানাও
        try:
            await app_bot.send_message(
                chat_id=chat_id,
                text=f"❌ {target_id} জ্যাম করতে সমস্যা হয়েছে: {e}"
            )
        except Exception:
            pass


# ─── Monitor loop ─────────────────────────────────────────────────────────────
async def _monitor_tick():
    if (not active_jams and not disconnected_jams) or not bot_app_instance:
        return

    logging.info(
        f"[monitor][DEBUG] tick শুরু — active_jams: {list(active_jams.keys())} | "
        f"disconnected_jams: {list(disconnected_jams.keys())}"
    )

    try:
        api = connect()
        active_users = list(api.path('ppp', 'active'))
        api.close()
    except Exception as e:
        logging.error(f"[monitor] রাউটার কানেক্ট হয়নি: {e}")
        return

    active_user_dict = {
        str(u.get('name', '')).strip(): u.get('address')
        for u in active_users
        if u.get('name') and u.get('address')
    }
    logging.info(f"[monitor][DEBUG] router থেকে পাওয়া active_user_dict: {active_user_dict}")
    app_bot = bot_app_instance.bot
    now = time.time()

    # ══ ১) বর্তমানে জ্যাম চলছে এমন ইউজারদের চেক (ডিসকানেক্ট / IP পরিবর্তন) ══
    for target_id, jam_info in list(active_jams.items()):
        if target_id == FULL_JAM_KEY:
            continue

        chat_id  = jam_info['chat_id']
        old_ip   = jam_info['current_ip']
        end_time = jam_info.get('end_time', now)

        # ── ডিসকানেক্ট ──
        if target_id not in active_user_dict:
            logging.info(f"[monitor] {target_id} ডিসকানেক্ট শনাক্ত হয়েছে।")
            jam_info['task'].cancel()

            try:
                api = connect()
                for e in list(api.path('ip', 'firewall', 'address-list')):
                    if e.get('address') == old_ip and e.get('comment') == USER_JAM_COMMENT:
                        api.path('ip', 'firewall', 'address-list').remove(e['.id'])
                api.close()
            except Exception as ex:
                logging.error(f"[monitor] ক্লিনআপ ব্যর্থ ({target_id}): {ex}")

            active_jams.pop(target_id, None)

            remaining_sec = end_time - now
            if remaining_sec > 0:
                # জ্যামের মেয়াদ এখনও বাকি → পেন্ডিং লিস্টে রাখো, কানেক্ট হলে আবার জ্যাম হবে
                disconnected_jams[target_id] = {
                    'chat_id': chat_id,
                    'end_time': end_time,
                    'old_ip': old_ip
                }
                note = (
                    f"⚠️ নোটিফিকেশন:\n"
                    f"জ্যাম থাকা অবস্থায় ইউজার {target_id}\n"
                    f"রাউটার থেকে ডিসকানেক্ট হয়ে গেছে!\n"
                    f"(পুরোনো IP: {old_ip})\n"
                    f"⏳ বাকি সময়: {int(remaining_sec)} সেকেন্ড\n"
                    f"🔁 ইউজার আবার কানেক্ট হলে বাকি সময়ের জন্য স্বয়ংক্রিয়ভাবে পুনরায় জ্যাম হয়ে যাবে।"
                )
            else:
                note = (
                    f"⚠️ নোটিফিকেশন:\n"
                    f"জ্যাম থাকা অবস্থায় ইউজার {target_id}\n"
                    f"রাউটার থেকে ডিসকানেক্ট হয়ে গেছে!\n"
                    f"(পুরোনো IP: {old_ip})"
                )

            try:
                await app_bot.send_message(chat_id=chat_id, text=note)
            except Exception as ex:
                logging.error(f"[monitor] ডিসকানেক্ট নোটিফাই ব্যর্থ ({target_id}): {ex}")

        else:
            # ── IP পরিবর্তন ──
            new_ip = active_user_dict[target_id]
            if new_ip == old_ip:
                continue

            logging.info(f"[monitor] {target_id} IP বদলেছে: {old_ip} → {new_ip}")
            jam_info['task'].cancel()

            # পুরোনো entry সরাও
            try:
                api = connect()
                for e in list(api.path('ip', 'firewall', 'address-list')):
                    if e.get('address') == old_ip and e.get('comment') == USER_JAM_COMMENT:
                        api.path('ip', 'firewall', 'address-list').remove(e['.id'])
                api.close()
            except Exception as ex:
                logging.error(f"[monitor] পুরোনো IP রিমুভ ব্যর্থ ({target_id}): {ex}")

            remaining_sec = max(end_time - now, 1)

            # নতুন IP-তে জ্যাম টাস্ক চালু
            new_task = asyncio.create_task(
                perform_jam(new_ip, target_id, remaining_sec, chat_id, app_bot)
            )
            active_jams[target_id] = {
                'task': new_task,
                'current_ip': new_ip,
                'chat_id': chat_id,
                'end_time': end_time
            }
            logging.info(f"[monitor] {target_id} রি-জ্যাম সম্পন্ন → {new_ip}")

            try:
                await app_bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔄 আইপি পরিবর্তন নোটিফিকেশন!\n\n"
                        f"👤 ইউজার: {target_id}\n"
                        f"🌐 পুরোনো IP: {old_ip}\n"
                        f"🌐 নতুন IP: {new_ip}\n"
                        f"⚠️ নতুন আইপিতে জ্যাম স্বয়ংক্রিয়ভাবে চালু হয়েছে!"
                    )
                )
            except Exception as ex:
                logging.error(f"[monitor] IP-change নোটিফাই ব্যর্থ ({target_id}): {ex}")

    # ══ ২) ডিসকানেক্টেড অবস্থায় থাকা জ্যামড ইউজাররা আবার active হলো কিনা চেক ══
    for target_id, info in list(disconnected_jams.items()):
        chat_id  = info['chat_id']
        end_time = info['end_time']
        old_ip   = info.get('old_ip')

        is_back = target_id in active_user_dict
        logging.info(
            f"[monitor][DEBUG] disconnected_jams চেক করছি → target_id={target_id!r}, "
            f"active_user_dict-এ আছে কিনা: {is_back}, বাকি সময়: {end_time - now:.1f}s"
        )

        if is_back:
            new_ip = active_user_dict[target_id]
            remaining_sec = end_time - now
            disconnected_jams.pop(target_id, None)

            if remaining_sec <= 0:
                # ডিসকানেক্ট অবস্থাতেই জ্যামের মেয়াদ শেষ হয়ে গেছে
                try:
                    await app_bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"ℹ️ নোটিফিকেশন:\n"
                            f"ইউজার {target_id} আবার নেটে active হয়েছে,\n"
                            f"তবে জ্যামের সময় ইতিমধ্যে শেষ হয়ে গেছে — তাই পুনরায় জ্যাম করা হয়নি।"
                        )
                    )
                except Exception as ex:
                    logging.error(f"[monitor] expiry নোটিফাই ব্যর্থ ({target_id}): {ex}")
                continue

            logging.info(f"[monitor] {target_id} আবার active হয়েছে → রি-জ্যাম শুরু ({new_ip})")

            # perform_jam নিজেই address-list এন্ট্রি ও filter rule বসায়,
            # তাই এখানে আলাদা করে সেটা করার দরকার নেই (ডুপ্লিকেট এন্ট্রি এড়াতে)।
            new_task = asyncio.create_task(
                perform_jam(new_ip, target_id, remaining_sec, chat_id, app_bot)
            )
            active_jams[target_id] = {
                'task': new_task,
                'current_ip': new_ip,
                'chat_id': chat_id,
                'end_time': end_time
            }

            try:
                await app_bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔁 নোটিফিকেশন:\n"
                        f"জ্যাম করা ইউজার {target_id} আবার নেটে active হয়েছে!\n"
                        f"🌐 নতুন IP: {new_ip}"
                        + (f" (আগের IP: {old_ip})" if old_ip and old_ip != new_ip else "") + "\n"
                        f"⏳ বাকি সময়: {int(remaining_sec)} সেকেন্ড\n"
                        f"✅ স্বয়ংক্রিয়ভাবে পুনরায় জ্যাম করা হয়েছে।"
                    )
                )
            except Exception as ex:
                logging.error(f"[monitor] reconnect নোটিফাই ব্যর্থ ({target_id}): {ex}")

        elif now >= end_time:
            # ডিসকানেক্ট অবস্থাতেই জ্যামের মেয়াদ শেষ — পরিষ্কার করে দাও
            disconnected_jams.pop(target_id, None)
            logging.info(f"[monitor] {target_id} এর পেন্ডিং জ্যাম মেয়াদ শেষে মুছে ফেলা হলো (এখনও ডিসকানেক্টেড)।")


async def monitor_active_jams():
    """
    প্রতি ২ সেকেন্ডে জ্যামড ইউজারের অবস্থা ট্র্যাক করে:
      • IP পরিবর্তন হলে         → নতুন IP-তে অটো রি-জ্যাম + নোটিফিকেশন
      • ডিসকানেক্ট হলে          → জ্যাম পজ, নোটিফিকেশন (মেয়াদ থাকলে disconnected_jams-এ রাখা হয়)
      • ডিসকানেক্টের পর আবার active হলে → বাকি সময়ের জন্য অটো রি-জ্যাম + নোটিফিকেশন

    গুরুত্বপূর্ণ: এই while-loop টা একবারই asyncio.create_task() দিয়ে চালু হয়
    (fire-and-forget)। প্রতিটা tick try/except দিয়ে ঘেরা, তাই কোনো একটা
    iteration-এ অপ্রত্যাশিত error হলেও পুরো মনিটরিং loop চিরতরে বন্ধ হয়ে
    যাবে না — এতদিন এই wrap ছাড়া একটা error হলেই IP-change ও reconnect
    নোটিফিকেশন আর কখনো আসত না।
    """
    while True:
        await asyncio.sleep(2)
        try:
            await _monitor_tick()
        except Exception as e:
            logging.error(f"[monitor] অপ্রত্যাশিত এরর (loop চলতেই থাকবে): {e}")


# ─── Text input handler ───────────────────────────────────────────────────────
async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get('step')
    chat_id = update.effective_chat.id

    # ══ PIN verify ══
    if step == 'waiting_for_pin':
        entered_pin = update.message.text.strip()
        if BOT_PIN and entered_pin == BOT_PIN:
            authorized_chats.add(chat_id)
            context.user_data['step'] = None
            await update.message.reply_text(
                "✅ PIN সঠিক! মাইক্রোটিক বট মেনু:",
                reply_markup=main_menu_keyboard()
            )
        else:
            await update.message.reply_text("❌ ভুল PIN! আবার চেষ্টা করুন:")
        return

    # PIN verify না করা chat থেকে অন্য কোনো ইনপুট এলে ব্লক করো
    if chat_id not in authorized_chats:
        await update.message.reply_text("🔒 প্রথমে /start চাপুন এবং PIN verify করুন।")
        return

    # ══ ইউজারনেম ইনপুট ══
    if step == 'waiting_for_ip':
        user_id = update.message.text.strip()
        try:
            api = connect()
            target_ip  = None
            uptime     = 'N/A'
            iface_name = None
            bytes_in   = 0
            bytes_out  = 0

            active_users  = list(api.path('ppp', 'active'))
            address_lists = list(api.path('ip', 'firewall', 'address-list'))

            for u in active_users:
                if str(u.get('name', '')).strip() == user_id:
                    target_ip  = u.get('address')
                    uptime     = u.get('uptime', 'N/A')
                    iface_name = u.get('interface') or user_id
                    break

            # Interface থেকে আসল rx/tx কাউন্টার
            # RouterOS-এ dynamic PPPoE interface-এর নাম মাঝেমধ্যে <pppoe-user>
            # (bracket সহ) আবার কখনো bracket ছাড়া রিপোর্ট হয় — তাই bracket বাদ
            # দিয়ে normalize করে এবং কয়েকটা সম্ভাব্য নামের সাথে মিলিয়ে খোঁজা হচ্ছে।
            if iface_name:
                def _norm(n):
                    return str(n or '').strip().strip('<>').strip()

                candidates = {
                    _norm(iface_name),
                    _norm(f"pppoe-{user_id}"),
                    _norm(user_id),
                }
                try:
                    matched_iface = None
                    for iface in api.path('interface'):
                        if _norm(iface.get('name')) in candidates:
                            matched_iface = iface
                            break
                    if matched_iface:
                        bytes_in  = int(matched_iface.get('rx-byte') or 0)
                        bytes_out = int(matched_iface.get('tx-byte') or 0)
                    else:
                        logging.warning(
                            f"[byte-stats] '{user_id}' এর জন্য interface মেলেনি। "
                            f"চেষ্টা করা নাম: {candidates}"
                        )
                except Exception as ex:
                    logging.error(f"interface stats fetch failed ({user_id}): {ex}")

            api.close()

        except Exception as e:
            await update.message.reply_text(f"❌ রাউটার কানেক্ট হয়নি: {e}")
            return

        if not target_ip:
            # plain text — username-এ _ থাকলে Markdown crash হত
            await update.message.reply_text(
                f"❌ ডিসকানেক্টেড: {user_id} বর্তমানে রাউটারে কানেক্টেড নেই।"
            )
            context.user_data['step'] = None
            return

        is_jammed = any(
            e.get('address') == target_ip and e.get('comment') == USER_JAM_COMMENT
            for e in address_lists
        )
        jam_status = "🔴 জ্যাম (Jammed)" if is_jammed else "🟢 সচল"

        # plain text — Markdown বাদ দিয়ে পাঠানো হচ্ছে
        await update.message.reply_text(
            f"👤 ইউজার   : {user_id}\n"
            f"🌐 IP      : {target_ip}\n"
            f"⏱️ আপটাইম : {uptime}\n"
            f"📥 Download: {format_bytes(bytes_out)}\n"
            f"📤 Upload  : {format_bytes(bytes_in)}\n"
            f"📶 অবস্থা  : {jam_status}\n\n"
            "⏱️ কতক্ষণ জ্যাম করবেন? (যেমন: 30s, 10m, 1h)"
        )
        context.user_data['target_ip'] = target_ip
        context.user_data['target_id'] = user_id
        context.user_data['step']      = 'waiting_for_time'
        return

    # ══ সময় ইনপুট — ইউজার জ্যাম ══
    elif step == 'waiting_for_time':
        sec = parse_time(update.message.text)
        if sec is None:
            await update.message.reply_text("⚠️ ভুল ফরম্যাট! যেমন: 30s, 10m বা 1h লিখুন।")
            return

        # বট রিস্টার্টের পরে context হারিয়ে গেলে KeyError থেকে রক্ষা
        target_ip = context.user_data.get('target_ip')
        target_id = context.user_data.get('target_id')
        if not target_ip or not target_id:
            await update.message.reply_text("❌ সেশন মেয়াদ শেষ। আবার 'ইউজার জ্যাম' বাটন চাপুন।")
            context.user_data['step'] = None
            return

        # আগে থেকে পেন্ডিং কিছু থাকলে সরিয়ে নতুন করে শুরু করো
        disconnected_jams.pop(target_id, None)
        if target_id in active_jams:
            active_jams[target_id]['task'].cancel()

        end_time = time.time() + sec
        task = asyncio.create_task(
            perform_jam(target_ip, target_id, sec, update.effective_chat.id, context.bot)
        )
        active_jams[target_id] = {
            'task': task,
            'current_ip': target_ip,
            'chat_id': update.effective_chat.id,
            'end_time': end_time
        }
        context.user_data['step'] = None

    # ══ পাসওয়ার্ড দেখা — ইউজারনেম ইনপুট ══
    elif step == 'pass_waiting_for_user':
        user_id = update.message.text.strip()
        try:
            api = connect()
            secrets = list(api.path('ppp', 'secret'))
            api.close()
        except Exception as e:
            await update.message.reply_text(f"❌ রাউটার কানেক্ট হয়নি: {e}")
            return

        found = None
        for s in secrets:
            if str(s.get('name', '')).strip() == user_id:
                found = s
                break

        if not found:
            await update.message.reply_text(
                f"❌ PPPoE secret-এ '{user_id}' নামের কোনো ইউজার পাওয়া যায়নি।"
            )
            context.user_data['step'] = None
            return

        current_pass    = found.get('password', '(খালি)')
        current_profile = found.get('profile',  'N/A')
        current_comment = found.get('comment',  '')

        kb = [
            [InlineKeyboardButton("✏️ পাসওয়ার্ড পরিবর্তন", callback_data=f"change_pass_{user_id}"),
             InlineKeyboardButton("📋 প্রোফাইল পরিবর্তন",  callback_data=f"change_profile_{user_id}")],
        ]
        await update.message.reply_text(
            f"👤 ইউজার     : {user_id}\n"
            f"🔑 পাসওয়ার্ড  : {current_pass}\n"
            f"📋 প্রোফাইল  : {current_profile}\n"
            f"💬 মন্তব্য    : {current_comment or '(নেই)'}",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        context.user_data['step'] = None
        return

    # ══ পাসওয়ার্ড পরিবর্তন — নতুন পাসওয়ার্ড ইনপুট ══
    elif step == 'pass_waiting_for_new':
        new_pass = update.message.text.strip()
        target_user = context.user_data.get('pass_target')

        if not target_user:
            await update.message.reply_text("❌ সেশন মেয়াদ শেষ। আবার 'পাসওয়ার্ড' বাটন চাপুন।")
            context.user_data['step'] = None
            return

        if not new_pass:
            await update.message.reply_text("⚠️ পাসওয়ার্ড খালি দেওয়া যাবে না।")
            return

        try:
            api = connect()
            secrets = list(api.path('ppp', 'secret'))
            secret_id = None
            for s in secrets:
                if str(s.get('name', '')).strip() == target_user:
                    secret_id = s.get('.id')
                    break

            if not secret_id:
                await update.message.reply_text(
                    f"❌ '{target_user}' PPPoE secret-এ পাওয়া যায়নি।"
                )
                api.close()
                context.user_data['step'] = None
                return

            api.path('ppp', 'secret').update(**{'.id': secret_id, 'password': new_pass})
            api.close()
        except Exception as e:
            await update.message.reply_text(f"❌ পাসওয়ার্ড পরিবর্তন ব্যর্থ: {e}")
            return

        context.user_data['step']        = None
        context.user_data['pass_target'] = None
        await update.message.reply_text(
            f"✅ পাসওয়ার্ড সফলভাবে পরিবর্তন হয়েছে!\n"
            f"👤 ইউজার      : {target_user}\n"
            f"🔑 নতুন পাসওয়ার্ড: {new_pass}"
        )
        return

    # ══ প্রোফাইল পরিবর্তন — নতুন প্রোফাইল ইনপুট (manually typed) ══
    elif step == 'profile_waiting_for_new':
        new_profile = update.message.text.strip()
        target_user = context.user_data.get('profile_target')

        if not target_user:
            await update.message.reply_text("❌ সেশন মেয়াদ শেষ। আবার 'পাসওয়ার্ড' বাটন চাপুন।")
            context.user_data['step'] = None
            return

        if not new_profile:
            await update.message.reply_text("⚠️ প্রোফাইল নাম খালি দেওয়া যাবে না।")
            return

        try:
            api = connect()
            secrets = list(api.path('ppp', 'secret'))
            secret_id = next((s['.id'] for s in secrets
                              if str(s.get('name', '')).strip() == target_user), None)
            if not secret_id:
                await update.message.reply_text(f"❌ '{target_user}' PPPoE secret-এ পাওয়া যায়নি।")
                api.close()
                context.user_data['step'] = None
                return

            api.path('ppp', 'secret').update(**{'.id': secret_id, 'profile': new_profile})
            api.close()
        except Exception as e:
            await update.message.reply_text(f"❌ প্রোফাইল পরিবর্তন ব্যর্থ: {e}")
            return

        context.user_data['step']           = None
        context.user_data['profile_target'] = None
        await update.message.reply_text(
            f"✅ প্রোফাইল সফলভাবে পরিবর্তন হয়েছে!\n"
            f"👤 ইউজার       : {target_user}\n"
            f"📋 নতুন প্রোফাইল: {new_profile}"
        )
        return

    # ══ সময় ইনপুট — পুরো নেট জ্যাম ══
    elif context.user_data.get('waiting_for_time'):
        sec = parse_time(update.message.text)
        if sec is None:
            await update.message.reply_text("⚠️ ভুল ফরম্যাট! যেমন: 30s, 10m বা 1h লিখুন।")
            return

        context.user_data['waiting_for_time'] = False

        # আগের full jam থাকলে বাতিল করো
        if FULL_JAM_KEY in active_jams:
            active_jams[FULL_JAM_KEY]['task'].cancel()
            del active_jams[FULL_JAM_KEY]

        task = asyncio.create_task(perform_full_jam(sec, update))
        active_jams[FULL_JAM_KEY] = {
            'task': task,
            'chat_id': update.effective_chat.id
        }


# ─── Main ─────────────────────────────────────────────────────────────────────
async def main_async():
    global bot_app_instance
    clear_old_rules()

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logging.info("🌐 Flask Web Server running in background thread.")

    app = Application.builder().token(os.getenv("BOT_TOKEN")).build()
    bot_app_instance = app

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))

    asyncio.create_task(monitor_active_jams())

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        while True:
            await asyncio.sleep(3600)


def main():
    asyncio.run(main_async())


if __name__ == '__main__':
    main()
