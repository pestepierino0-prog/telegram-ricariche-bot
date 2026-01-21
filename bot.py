import os
from dotenv import load_dotenv
import telebot

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "Ciao! ✅ Bot online.\nScrivi /id")

@bot.message_handler(commands=["id"])
def get_id(message):
    bot.reply_to(message, f"chat_id: <code>{message.chat.id}</code>\nuser_id: <code>{message.from_user.id}</code>")

@bot.message_handler(func=lambda m: True)
def echo(message):
    bot.reply_to(message, "✅ Ricevuto. (Bot attivo)")

if __name__ == "__main__":
    print("Bot running...")
    bot.infinity_polling(skip_pending=True)
