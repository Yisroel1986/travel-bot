import os
import logging
import openai

from dotenv import load_dotenv
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

# Включаем логирование (по желанию, но полезно)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения из .env
load_dotenv()

# Считываем токен из .env
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Назначаем ключ OpenAI
openai.api_key = OPENAI_API_KEY

# Для отладки (убедиться, что реально что-то считалось):
print("DEBUG BOT_TOKEN =", BOT_TOKEN)

# Обработчик команды /start
def start(update, context):
    update.message.reply_text("Привет! Я бот на python-telegram-bot 13.15.\nЧем могу помочь?")

def ask_gpt(update, context):
    user_text = update.message.text
    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Ты – умный помощник, отвечай лаконично, но точно."},
                {"role": "user", "content": user_text},
            ],
            max_tokens=1000,
            temperature=0.7,
        )
        answer = response["choices"][0]["message"]["content"]
        update.message.reply_text(answer.strip())

    except Exception as e:
        update.message.reply_text(f"Ошибка при запросе к OpenAI: {e}")

# (Необязательно) Обработчик /help
def help_command(update, context):
    update.message.reply_text("Список доступных команд:\n"
                              "/start - начать\n"
                              "/help - помощь")

def main():
    """Основная точка входа в программу."""
    # Проверяем, что токен не пуст
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set!")
        # Вместо return (выйти из функции) можно завершить программу
        import sys
        sys.exit(1)
    if not OPENAI_API_KEY:
        print("Warning: OPENAI_API_KEY is not set!")
        return
    # Создаём Updater и берём токен
    updater = Updater(BOT_TOKEN, use_context=True)

    # Получаем диспетчер (dispatcher) для регистрации хендлеров
    dp = updater.dispatcher

    # Регистрируем обработчики команд
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, ask_gpt))

    # Запускаем бота
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
