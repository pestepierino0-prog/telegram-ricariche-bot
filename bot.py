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
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# ======================
# ENV
# ======================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_GROUP_ID = int(os.getenv("ALLOWED_GROUP_ID", "0").strip())
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0").strip())

# NEW: canale dove arrivano le notifiche (tu + bot)
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
OPEN_FROM = dtime(0, 0)
OPEN_TO = dtime(23, 59)

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


load_data()


# ======================
# USER COMMANDS
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
        bot.reply_to(message, "⚠️ Per privacy (ricevute), la ricarica si fa SOLO in privato col bot.")
        return

    if not member_of_group(uid):
        bot.reply_to(message, "⛔ Accesso negato. Disponibile solo per utenti nel gruppo autorizzato.")
        return

    if not is_open_hours():
        now = italy_now().strftime("%H:%M")
        bot.reply_to(message, f"⏳ Richieste chiuse ora.\nOra Italia: <b>{now}</b>\nDisponibile 10:00–23:00.")
        return

    reset_state(uid)
    st = ensure_state(uid)
    st.user_id = uid
    st.user_name = display_user(message.from_user)
    st.step = STEP_CHOOSE
    st.touch()

    bot.reply_to(message, "Perfetto ✅\nPremi il tasto qui sotto per iniziare:", reply_markup=kb_start())


# ======================
# ADMIN COMMANDS (optional keep)
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
    if not is_private_chat(message):
        return
    if not is_admin(message.from_user.id):
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        bot.reply_to(message, "Uso: <code>/link USER_ID https://...</code>")
        return

    uid = int(parts[1])
    url = parts[2].strip()

    bot.send_message(
        uid,
        "💳 <b>Link pagamento carta</b>\n"
        f"{url}\n\n"
        "Dopo il pagamento, rispondi qui con <b>OK</b>.\n"
        "⏱️ Entro 15 minuti la ricarica viene effettuata."
    )

    recs = history.get(uid, [])
    for r in reversed(recs):
        if r.payment == PAY_CARD and r.status == "pending":
            r.status = "link_sent"
            save_data()
            break

    bot.reply_to(message, "✅ Link inviato al cliente.")


# ======================
# FLOW CALLBACKS
# ======================
@bot.callback_query_handler(func=lambda c: c.data in {"start_recharge", "pay_carta", "pay_bitnovo", "confirm_yes", "confirm_edit", "confirm_cancel"})
def flow_callbacks(call):
    uid = call.from_user.id
    if call.message and call.message.chat.type != "private":
        bot.answer_callback_query(call.id, "Aprimi in privato per continuare.")
        return

    st = ensure_state(uid)

    def guard() -> bool:
        if not member_of_group(uid):
            bot.answer_callback_query(call.id, "Accesso negato (non sei nel gruppo).")
            return False
        if not is_open_hours():
            bot.answer_callback_query(call.id, "Fuori orario (10:00–23:00 Italia).")
            return False
        return True

    data = call.data

    if data == "start_recharge":
        if not guard():
            return
        st.step = STEP_ASK_CREDITS
        st.touch()
        bot.answer_callback_query(call.id)
        bot.send_message(call.message.chat.id, "Quanti <b>crediti</b> vuoi ricaricare? (scrivi solo il numero)")
        return

    if data in {"pay_carta", "pay_bitnovo"}:
        if not guard():
            return
        if st.step != STEP_CHOOSE_PAYMENT:
            bot.answer_callback_query(call.id, "Procedura non valida. Usa /ricarica.")
            return

        st.payment = PAY_CARD if data == "pay_carta" else PAY_BITNOVO
        st.step = STEP_ASK_PANEL
        st.touch()

        bot.answer_callback_query(call.id)
        label = "Carta (link)" if st.payment == PAY_CARD else "Bitcoin / Bitnovo"
        bot.send_message(call.message.chat.id, f"Hai scelto <b>{label}</b> ✅\n\nOra inserisci lo <b>username del tuo pannello</b>.")
        return

    if data in {"confirm_yes", "confirm_edit", "confirm_cancel"}:
        if not guard():
            return
        if st.step != STEP_CONFIRM:
            bot.answer_callback_query(call.id, "Procedura non valida. Usa /ricarica.")
            return

        bot.answer_callback_query(call.id)

        if data == "confirm_cancel":
            reset_state(uid)
            bot.send_message(call.message.chat.id, "❌ Procedura annullata. Riparti con <b>/ricarica</b>.")
            return

        if data == "confirm_edit":
            st.step = STEP_ASK_CREDITS
            st.credits = None
            st.payment = None
            st.panel_username = None
            st.receipt_file_id = None
            st.touch()
            bot.send_message(call.message.chat.id, "Ok ✏️ Riscrivimi quanti <b>crediti</b> vuoi ricaricare (solo numero).")
            return

        # confirm_yes
        if st.payment == PAY_BITNOVO:
            st.step = STEP_WAIT_RECEIPT
            st.touch()
            bot.send_message(call.message.chat.id, "Perfetto ✅\nAdesso <b>allega la foto dello scontrino Bitnovo</b> qui in chat.")
            return

        # CARD
        st.step = STEP_DONE
        st.touch()
        rec = create_record_from_state(st)
        push_history(rec)
        send_to_admin_channel(rec)
        bot.send_message(
            call.message.chat.id,
            "Perfetto ✅\nPer pagamento con <b>carta</b>: <b>aspetta</b> che genero io il link per pagare.\n\n"
            "⏱️ <b>Entro 15 minuti</b> la ricarica viene effettuata."
        )
        return


# ======================
# BUTTON in admin channel: "✅ Ricarica eseguita"
# ======================
@bot.callback_query_handler(func=lambda c: c.data.startswith("donebtn:"))
def cb_done_button(call):
    if call.from_user.id != ADMIN_USER_ID:
        bot.answer_callback_query(call.id, "Non autorizzato.")
        return

    parts = call.data.split(":")
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "Dati non validi.")
        return

    customer_id = int(parts[1])
    record_id = parts[2]

    # notify customer
    bot.send_message(customer_id, "✅ <b>Ricarica completata</b>\nGrazie! Se ti serve altro, scrivimi qui 👋")

    # update history
    recs = history.get(customer_id, [])
    for r in reversed(recs):
        if r.record_id == record_id:
            r.status = "completed"
            save_data()
            break

    # try to mark message completed (remove keyboard + add tag)
    try:
        if getattr(call.message, "caption", None):
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=(call.message.caption or "") + "\n\n✅ <b>COMPLETATA</b>",
                reply_markup=None
            )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(call.message.text or "") + "\n\n✅ <b>COMPLETATA</b>",
                reply_markup=None
            )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "✅ Cliente avvisato!")


# ======================
# TEXT ROUTER
# ======================
@bot.message_handler(content_types=["text"])
def text_router(message):
    uid = message.from_user.id
    if not is_private_chat(message):
        return

    st = ensure_state(uid)
    if st.step == STEP_IDLE:
        return

    if not member_of_group(uid):
        bot.reply_to(message, "⛔ Accesso negato (solo membri gruppo).")
        reset_state(uid)
        return

    if not is_open_hours():
        now = italy_now().strftime("%H:%M")
        bot.reply_to(message, f"⏳ Richieste chiuse ora. Ora Italia: <b>{now}</b> (10:00–23:00).")
        reset_state(uid)
        return

    txt = (message.text or "").strip()

    if st.step == STEP_ASK_CREDITS:
        if not re.fullmatch(r"\d{1,6}", txt):
            bot.reply_to(message, "Scrivi solo un numero (es: 10, 50, 100).")
            return
        credits = int(txt)
        if credits <= 0:
            bot.reply_to(message, "Inserisci un numero maggiore di 0.")
            return

        st.credits = credits
        st.step = STEP_CHOOSE_PAYMENT
        st.touch()

        bot.reply_to(
            message,
            f"Ok ✅ Vuoi ricaricare: <b>{credits}</b> crediti.\n\nScegli il <b>metodo di pagamento</b>:",
            reply_markup=kb_payments()
        )
        return

    if st.step == STEP_ASK_PANEL:
        if len(txt) < 3:
            bot.reply_to(message, "Inserisci uno username valido del pannello.")
            return

        st.panel_username = txt
        st.step = STEP_CONFIRM
        st.touch()

        bot.reply_to(message, format_summary(st) + "\nConfermi questi dati?", reply_markup=kb_confirm())
        return

    if st.step == STEP_WAIT_RECEIPT:
        bot.reply_to(message, "📸 Sto aspettando la <b>foto dello scontrino Bitnovo</b>. Invia una foto qui.")
        return

    if st.step == STEP_DONE:
        bot.reply_to(message, "✅ Richiesta già presa in carico.")
        return


# ======================
# PHOTO ROUTER (Bitnovo)
# ======================
@bot.message_handler(content_types=["photo"])
def photo_router(message):
    uid = message.from_user.id
    if not is_private_chat(message):
        return

    st = ensure_state(uid)
    if st.step != STEP_WAIT_RECEIPT:
        return

    if not member_of_group(uid):
        bot.reply_to(message, "⛔ Accesso negato (solo membri gruppo).")
        reset_state(uid)
        return

    if not is_open_hours():
        bot.reply_to(message, "⏳ Fuori orario (10:00–23:00 Italia). Riprova domani.")
        reset_state(uid)
        return

    file_id = message.photo[-1].file_id
    st.receipt_file_id = file_id
    st.step = STEP_DONE
    st.touch()

    rec = create_record_from_state(st)
    push_history(rec)
    send_to_admin_channel(rec)

    bot.reply_to(message, "Ricevuto ✅\n⏱️ <b>Entro 15 minuti</b> la ricarica viene effettuata.\nA presto 👋")


# ======================
# RECORD + SEND TO ADMIN CHANNEL
# ======================
def create_record_from_state(st: RechargeState) -> RechargeRecord:
    rec_id = new_record_id(st.user_id or 0)
    created = italy_now().isoformat()
    return RechargeRecord(
        record_id=rec_id,
        created_at_iso=created,
        user_id=st.user_id or 0,
        username=st.user_name or f"user_id {st.user_id}",
        credits=st.credits or 0,
        payment=st.payment or "",
        panel_username=st.panel_username or "",
        receipt_file_id=st.receipt_file_id,
        status="pending",
    )


def send_to_admin_channel(rec: RechargeRecord):
    pay_label = "Carta (link)" if rec.payment == PAY_CARD else "Bitcoin / Bitnovo"

    text = (
        "🧾 <b>NUOVA RICARICA</b>\n\n"
        f"🆔 Record: <code>{rec.record_id}</code>\n"
        f"🕒 Italia: <b>{rec.created_at_iso.split('T')[0]} {rec.created_at_iso.split('T')[1][:5]}</b>\n"
        f"👤 Utente: <b>{rec.username}</b>\n"
        f"🔢 Crediti: <b>{rec.credits}</b>\n"
        f"💳 Metodo: <b>{pay_label}</b>\n"
        f"🧩 Pannello: <code>{rec.panel_username}</code>\n"
        f"👤 user_id: <code>{rec.user_id}</code>\n"
    )

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Ricarica eseguita", callback_data=f"donebtn:{rec.user_id}:{rec.record_id}"))

    if rec.payment == PAY_CARD:
        text += (
            "\n➡️ <b>Azione:</b> genera link carta e invialo al cliente:\n"
            f"<code>/link {rec.user_id} https://...</code>\n"
        )
        bot.send_message(ADMIN_NOTIFY_CHAT_ID, text, reply_markup=kb)
        return

    text += "\n➡️ <b>Azione:</b> verifica scontrino Bitnovo e ricarica."
    bot.send_photo(ADMIN_NOTIFY_CHAT_ID, rec.receipt_file_id, caption=text, reply_markup=kb)


# ======================
# RUN
# ======================
if __name__ == "__main__":
    print("Bot running...")
    bot.infinity_polling(skip_pending=True)
