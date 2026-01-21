import os
import re
import json
from dataclasses import dataclass, asdict
from datetime import datetime, time as dtime, timedelta
from typing import Dict, Optional, List

from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:
    ZoneInfo = None

# ======================
# ENV
# ======================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_GROUP_ID = int(os.getenv("ALLOWED_GROUP_ID", "0").strip())
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0").strip())

# NEW: notifications go to this chat (your private channel where bot is admin)
ADMIN_NOTIFY_CHAT_ID = int(os.getenv("ADMIN_NOTIFY_CHAT_ID", "0").strip())

STATE_TIMEOUT_MIN = int(os.getenv("STATE_TIMEOUT_MIN", "20").strip())
DATA_FILE = os.getenv("DATA_FILE", "recharges.json").strip()

if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")
if ALLOWED_GROUP_ID == 0:
    raise RuntimeError("Missing ALLOWED_GROUP_ID")
if ADMIN_USER_ID == 0:
    raise RuntimeError("Missing ADMIN_USER_ID")
if ADMIN_NOTIFY_CHAT_ID == 0:
    raise RuntimeError("Missing ADMIN_NOTIFY_CHAT_ID")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# ======================
# CONFIG
# ======================
ITALY_TZ_NAME = "Europe/Rome"

# PRODUCTION HOURS:
OPEN_FROM = dtime(0, 0)
OPEN_TO = dtime(23, 59.)

# Steps
STEP_IDLE = "idle"
STEP_CHOOSE = "choose"
STEP_ASK_CREDITS = "ask_credits"
STEP_CHOOSE_PAYMENT = "choose_payment"
STEP_ASK_PANEL = "ask_panel"
STEP_CONFIRM = "confirm"
STEP_WAIT_RECEIPT = "wait_receipt"
STEP_DONE = "done"

PAY_CARD = "carta"
PAY_BITNOVO = "bitnovo"

# ======================
# STATE / STORAGE
# ======================
@dataclass
class RechargeState:
    step: str = STEP_IDLE
    credits: Optional[int] = None
    payment: Optional[str] = None
    panel_username: Optional[str] = None
    receipt_file_id: Optional[str] = None
    created_at_iso: Optional[str] = None
    updated_at_iso: Optional[str] = None

    user_id: Optional[int] = None
    user_name: Optional[str] = None

    def touch(self):
        now = italy_now().isoformat()
        if not self.created_at_iso:
            self.created_at_iso = now
        self.updated_at_iso = now


@dataclass
class RechargeRecord:
    record_id: str
    created_at_iso: str
    user_id: int
    username: str
    credits: int
    payment: str
    panel_username: str
    receipt_file_id: Optional[str] = None
    status: str = "pending"  # pending | link_sent | completed


user_state: Dict[int, RechargeState] = {}
history: Dict[int, List[RechargeRecord]] = {}

# ======================
# TIME / HELPERS
# ======================
def italy_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(ITALY_TZ_NAME))
    return datetime.now()

def is_open_hours() -> bool:
    t = italy_now().time()
    return OPEN_FROM <= t < OPEN_TO

def is_private_chat(message) -> bool:
    return message.chat.type == "private"

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_USER_ID

def safe_username(user) -> str:
    return f"@{user.username}" if getattr(user, "username", None) else "(no_username)"

def display_user(user) -> str:
    name = (user.first_name or "").strip()
    last = (user.last_name or "").strip()
    full = (name + " " + last).strip()
    return f"{full} {safe_username(user)}".strip()

def load_data():
    global history
    if not os.path.exists(DATA_FILE):
        return
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        history = {}
        for uid_str, records in raw.get("history", {}).items():
            uid = int(uid_str)
            history[uid] = [RechargeRecord(**r) for r in records]
    except Exception as e:
        print("Failed to load data:", e)

def save_data():
    try:
        serializable = {
            "history": {str(uid): [asdict(r) for r in recs] for uid, recs in history.items()}
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Failed to save data:", e)

def member_of_group(user_id: int) -> bool:
    """
    Bot must be in the allowed group; better as admin.
    """
    try:
        m = bot.get_chat_member(ALLOWED_GROUP_ID, user_id)
        return m.status in ("member", "administrator", "creator")
    except Exception:
        return False

def expired(st: RechargeState) -> bool:
    if not st.updated_at_iso:
        return False
    try:
        last = datetime.fromisoformat(st.updated_at_iso)
    except Exception:
        return False
    return (italy_now() - last) > timedelta(minutes=STATE_TIMEOUT_MIN)

def reset_state(user_id: int):
    user_state[user_id] = RechargeState(step=STEP_IDLE, user_id=user_id)

def ensure_state(user_id: int) -> RechargeState:
    st = user_state.get(user_id)
    if not st:
        st = RechargeState(step=STEP_IDLE, user_id=user_id)
        user_state[user_id] = st
    if st.step != STEP_IDLE and expired(st):
        reset_state(user_id)
        st = user_state[user_id]
    return st

def kb_start() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔋 Ricarica crediti", callback_data="start_recharge"))
    return kb

def kb_payments() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("💳 Carta (link)", callback_data="pay_carta"),
        InlineKeyboardButton("₿ Bitcoin / Bitnovo", callback_data="pay_bitnovo"),
    )
    return kb

def kb_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("✅ Confermo", callback_data="confirm_yes"),
        InlineKeyboardButton("✏️ Modifica", callback_data="confirm_edit"),
        InlineKeyboardButton("❌ Annulla", callback_data="confirm_cancel"),
    )
    return kb

def format_summary(st: RechargeState) -> str:
    pay_label = "Carta (link)" if st.payment == PAY_CARD else "Bitcoin / Bitnovo"
    return (
        "📋 <b>Riepilogo</b>\n"
        f"• Crediti: <b>{st.credits}</b>\n"
        f"• Metodo: <b>{pay_label}</b>\n"
        f"• Username pannello: <code>{st.panel_username}</code>\n"
    )

def new_record_id(user_id: int) -> str:
    ts = italy_now().strftime("%Y%m%d-%H%M%S")
    return f"{user_id}-{ts}"

def push_history(rec: RechargeRecord):
    history.setdefault(rec.user_id, []).append(rec)
    history[rec.user_id] = history[rec.user_id][-50:]
    save_data()

def get_last_records(user_id: int, n: int = 5) -> List[RechargeRecord]:
    return list(history.get(user_id, []))[-n:]

# ======================
# BOOT
# ======================
load_data()

# ======================
# COMMANDS (User)
# ======================
@bot.message_handler(commands=["start"])
def cmd_start(message):
    if not is_private_chat(message):
        return
    bot.reply_to(message, "Ciao! 👋\nPer avviare scrivi <b>/ricarica</b>.")

@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(
        message,
        f"chat_id: <code>{message.chat.id}</code>\nuser_id: <code>{message.from_user.id}</code>"
    )

@bot.message_handler(commands=["ricarica"])
def cmd_ricarica(message):
    uid = message.from_user.id

    if not is_private_chat(message):
        bot.reply_to(
            message,
            "⚠️ Per privacy (ricevute), la ricarica si fa SOLO in chat privata con me.\n"
            "Aprimi e scrivi: <b>/ricarica</b>"
        )
        return

    if not member_of_group(uid):
        bot.reply_to(
            message,
            "⛔ Accesso negato.\n"
            "Questo servizio è disponibile solo per gli utenti presenti nel gruppo autorizzato."
        )
        return

    if not is_open_hours():
        now = italy_now().strftime("%H:%M")
        bot.reply_to(
            message,
            f"⏳ Richieste chiuse ora.\nOra Italia: <b>{now}</b>\nDisponibile 10:00–23:00."
        )
        return

    reset_state(uid)
    st = ensure_state(uid)
    st.user_id = uid
    st.user_name = display_user(message.from_user)
    st.step = STEP_CHOOSE
    st.touch()

    bot.reply_to(message, "Perfetto ✅\nPremi il tasto qui sotto per iniziare:", reply_markup=kb_start())

# ======================
# COMMANDS (Admin)
# ======================
@bot.message_handler(commands=["history"])
def cmd_history(message):
    if not is_private_chat(message):
        return
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Uso: <code>/history USER_ID</code>")
        return

    uid = int(parts[1])
    recs = get_last_records(uid, n=8)
    if not recs:
        bot.reply_to(message, "Nessuna richiesta trovata per questo utente.")
        return

    lines = ["🗂️ <b>Ultime richieste</b>"]
    for r in recs:
        pay = "Carta" if r.payment == PAY_CARD else "Bitnovo"
        lines.append(
            f"\n• <b>{r.record_id}</b>\n"
            f"  Data: {r.created_at_iso.split('T')[0]} {r.created_at_iso.split('T')[1][:5]}\n"
            f"  Crediti: {r.credits} | Metodo: {pay} | Stato: {r.status}\n"
            f"  Pannello: <code>{r.panel_username}</code>"
        )
    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["link"])
def cmd_link(message):
    """
    Admin sends card payment link to customer:
    /link <user_id> <url>
    """
    if not is_private_chat(message):
        return
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Uso: <code>/link USER_ID https://...</code>")
        return
    if not parts[1].isdigit():
        bot.reply_to(message, "USER_ID non valido.")
        return

    uid = int(parts[1])
    url = parts[2].strip()

    try:
        bot.send_message(
            uid,
            "💳 <b>Link pagamento carta</b>\n"
            f"{url}\n\n"
            "Dopo il pagamento, rispondi qui con <b>OK</b> oppure scrivimi se hai problemi.\n"
            "⏱️ Entro 15 minuti la ricarica viene effettuata."
        )
    except Exception as e:
        bot.reply_to(message, f"Errore invio al cliente: {e}")
        return

    recs = history.get(uid, [])
    for r in reversed(recs):
        if r.payment == PAY_CARD and r.status == "pending":
            r.status = "link_sent"
            save_data()
            break

    bot.reply_to(message, "✅ Link inviato al cliente.")

@bot.m
