import os
import logging
import sys
import psutil
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ChatAction

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

import openai
from datetime import timezone, timedelta, datetime
from flask import Flask, request
import asyncio
import threading
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from googletrans import Translator
from langdetect import detect
import json
import random

#
# ------------------ ЛОГИРОВАНИЕ ------------------
#
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

#
# ------------------ ЗАГРУЗКА .ENV ----------------
#
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")

# Ключ для OpenAI
openai.api_key = OPENAI_API_KEY

#
# ------------ КОНСТАНТЫ ДЛЯ СОСТОЯНИЙ ------------
#
(
    STAGE_INTRO,
    STAGE_NEEDS,
    STAGE_PRESENTATION,
    STAGE_ADDITIONAL_QUESTIONS,
    STAGE_FEEDBACK,
    STAGE_CLOSE,
    STAGE_ENDLESS,  # <-- последний этап, но мы не завершаем разговор
) = range(7)

#
# ----------- ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---------------
#
bot_loop = None
active_chats = {}

#
# ------------------ ПЕРЕВОДЧИК, SENTIMENT -----------
#
translator = Translator()

logger.info("Ініціалізація VADER Sentiment Analyzer...")
sentiment_analyzer = SentimentIntensityAnalyzer()
logger.info("VADER Sentiment Analyzer ініціалізований.")

#
# ---------------- МОДЕЛЬ ДАННЫХ ----------------
#
class ChatContext:
    def __init__(self):
        self.history = []
        self.user_info = {}
        self.current_stage = STAGE_INTRO
        self.last_interaction = datetime.now()
        self.sentiment_history = []
        self.needs_step = 1
        self.presentation_step = 1

    def add_message(self, role: str, content: str, sentiment: str = None):
        """
        Сохраняем историю последних 10 сообщений, чтобы GPT знал контекст.
        """
        self.history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "sentiment": sentiment
        })
        if len(self.history) > 10:
            self.history.pop(0)

    def update_user_info(self, key: str, value: str):
        self.user_info[key] = value

    def get_context_summary(self) -> str:
        """
        Возвращаем последние 3 сообщения для контекста GPT.
        """
        if not self.history:
            return "Початок розмови"
        recent_messages = self.history[-3:]
        summary = []
        for msg in recent_messages:
            role = msg["role"]
            content = msg["content"]
            summary.append(f"{role}: {content}")
        return "\n".join(summary)

def get_chat_context(chat_id: int) -> ChatContext:
    if chat_id not in active_chats:
        active_chats[chat_id] = ChatContext()
    return active_chats[chat_id]

#
# ----------------- ПРОВЕРКА ЗАПУСКА БОТА -----------
#
def is_bot_already_running():
    current_process = psutil.Process()
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.info["name"] == current_process.name() and \
           process.info["cmdline"] == current_process.cmdline() and \
           process.info["pid"] != current_process.pid:
            return True
    return False

#
# -------------------- SENTIMENT ANALYSIS -------------------
#
async def analyze_sentiment(text: str) -> str:
    try:
        scores = sentiment_analyzer.polarity_scores(text)
        compound = scores["compound"]
        if compound >= 0.05:
            return "позитивний"
        elif compound <= -0.05:
            return "негативний"
        else:
            return "нейтральний"
    except Exception as e:
        logger.error(f"Sentiment error: {e}")
        return "нейтральний"

#
# ----------------- GPT ФУНКЦИЯ -----------------
#
async def invoke_gpt(stage: str, user_text: str, context: ChatContext) -> str:
    system_prompt = f"""
    Ти — досвідчений менеджер з продажу турів, Марія. 
    Спілкуєшся з клієнтом щодо туру до зоопарку Ньїредьгаза.
    
    Контекст розмови:
    - Етап: {stage}
    - Історія: {context.get_context_summary()}
    - Інформація про клієнта: {json.dumps(context.user_info, ensure_ascii=False)}
    
    Правила спілкування:
    1. Відповідай природно, як жива людина, будь максимально розумною.
    2. Використовуй емпатію та емоційний інтелект.
    3. Задавай відкриті питання, щоб вести клієнта до продажу.
    4. Створюй відчуття терміновості і соціальні докази.
    5. Якщо клієнт відхиляється від теми, намагайся підтримати розмову і все одно повернути його до покупки.
    6. Ніколи не завершуй розмову самостійно, завжди готовий відповісти на все.
    7. Спілкуйся українською мовою, коротко та дружньо.
    """

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_text}
    ]
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=1200,
            temperature=0.9
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"GPT error: {e}")
        return "Вибачте, сталася помилка. Я готова продовжити розмову!"

#
# ------------------ ИМИТАЦИЯ ПЕЧАТИ ------------------
#
async def natural_typing_delay(text: str) -> float:
    base_delay = len(text) * 0.05
    variance = base_delay * 0.2
    delay = base_delay + random.uniform(-variance, variance)
    return min(6.0, max(1.0, delay))

async def simulate_typing(update: Update, text_len: int):
    typing_start = datetime.now()
    # "Фейковая" строка для расчёта
    typedelay = await natural_typing_delay(" " * text_len)

    while (datetime.now() - typing_start).total_seconds() < typedelay:
        await update.effective_chat.send_action(ChatAction.TYPING)
        await asyncio.sleep(1.0)

async def send_message_with_typing(update: Update, text: str):
    await simulate_typing(update, len(text))
    await update.message.reply_text(text)

#
# ----------------  CANCEL -----------------
#
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in active_chats:
        del active_chats[chat_id]

    text = "Розумію, якщо вам потрібен час. Якщо передумаєте, просто напишіть /start!"
    await send_message_with_typing(update, text)
    return ConversationHandler.END

#
# ---------------- OSНОВНЫЕ ЭТАПЫ -------------
#
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_context = get_chat_context(update.effective_chat.id)
    chat_context.current_stage = STAGE_NEEDS

    user_name = update.effective_user.first_name or "друже"
    greeting = (
        f"Вітаю, {user_name}! Я Марія, ваш менеджер з Family Place. "
        "Бачу, що ви цікавитесь туром до зоопарку Ньїредьгаза. "
        "Підкажіть, з якого міста плануєте виїжджати?"
    )
    chat_context.add_message("bot", greeting)
    await send_message_with_typing(update, greeting)
    return STAGE_NEEDS

async def needs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Сбор информации: город, сколько людей, даты, и т.д.
    """
    chat_context = get_chat_context(update.effective_chat.id)
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    step = chat_context.needs_step

    if step == 1:
        chat_context.update_user_info("departure_city", user_text)
        reply = "Чудово! А скільки людей поїде з вами? Чи будуть діти?"
        chat_context.needs_step = 2
    elif step == 2:
        chat_context.update_user_info("group_size", user_text)
        reply = "Зрозуміла! На які дати орієнтуєтесь?"
        chat_context.needs_step = 3
    elif step == 3:
        chat_context.update_user_info("dates", user_text)
        reply = (
            "Чудово, маю для вас цікаву пропозицію. "
            "Хотіли б почути деталі туру?"
        )
        chat_context.needs_step = 4
    elif step == 4:
        # Если согласен - идём презентация
        if "да" in user_text.lower() or "так" in user_text.lower() or "хочу" in user_text.lower():
            return await presentation_handler(update, context)
        else:
            # Не согласился
            reply = "Розумію. Можливо, у вас є запитання чи сумніви?"
    else:
        # Fallback, ask GPT
        reply = await invoke_gpt("needs", user_text, chat_context)

    chat_context.add_message("bot", reply)
    await send_message_with_typing(update, reply)
    return STAGE_NEEDS

async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Бот презентует тур, указывает цену и преимущества.
    """
    chat_context = get_chat_context(update.effective_chat.id)
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    city = chat_context.user_info.get("departure_city", "вашого міста")
    group = chat_context.user_info.get("group_size", "2-3 осіб")

    text = (
        f"Для групи з {group} з міста {city} пропонуємо зручний тур:\n\n"
        "👉 Трансфер, квитки, страхування і супровід — усе включено.\n"
        "👉 Додатково: дитячі розваги і екскурсія.\n\n"
        "Вартість: 2000 грн/особи.\n"
        "Діє акція: якщо бронюєте до кінця тижня — знижка 10%!\n\n"
        "Як вам таке? Готові обговорити подробиці?"
    )
    chat_context.add_message("bot", text)
    await send_message_with_typing(update, text)
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Отвечаем на любые дополнительные вопросы. 
    Если пользователь готов к оплате - переводим в STAGE_CLOSE.
    """
    chat_context = get_chat_context(update.effective_chat.id)
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    # Простой триггер, если юзер хочет оплачивать
    if any(word in user_text.lower() for word in ["оплат", "купити", "бронювати"]):
        text = (
            "Чудово! Тоді можемо перейти до оформлення і оплати. Готові?"
        )
        chat_context.add_message("bot", text)
        await send_message_with_typing(update, text)
        return STAGE_CLOSE
    else:
        # GPT-ответ
        gpt_reply = await invoke_gpt("additional_questions", user_text, chat_context)
        chat_context.add_message("bot", gpt_reply)
        await send_message_with_typing(update, gpt_reply)
        # Не завершаем, остаемся в STAGE_ADDITIONAL_QUESTIONS
        return STAGE_ADDITIONAL_QUESTIONS

async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    В классическом сценарии тут спрашиваем "Как вам предложение?". 
    Но по условию у нас нет явного перехода в feedback. 
    """
    chat_context = get_chat_context(update.effective_chat.id)
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    # Пример
    text = "Чудово, чи подобається вам ідея такої подорожі? Ви готові зробити бронювання?"
    chat_context.add_message("bot", text)
    await send_message_with_typing(update, text)
    return STAGE_CLOSE

async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Этап «Закрытие сделки». Если «да» → даём реквизиты. Если «нет» → уговариваем дальше.
    """
    chat_context = get_chat_context(update.effective_chat.id)
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    # Если юзер говорит "да" → дать реквизиты
    if "да" in user_text.lower() or "так" in user_text.lower() or "хочу" in user_text.lower():
        text = (
            "Супер! Тоді ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000\n"
            "Отримувач: Family Place\n\n"
            "Після оплати надішліть, будь ласка, скрін. Якщо є питання — я на зв'язку!"
        )
        chat_context.add_message("bot", text)
        await send_message_with_typing(update, text)
        # Но мы не завершаем разговор — переводим в «вечный» этап
        return STAGE_ENDLESS
    else:
        text = (
            "Розумію, можливо, у вас є сумніви чи уточнення? "
            "Я можу відповісти на будь-які питання або показати відгуки інших клієнтів!"
        )
        chat_context.add_message("bot", text)
        await send_message_with_typing(update, text)
        return STAGE_CLOSE

async def endless_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    «Бесконечный» этап после закрытия сделки. 
    Если пользователь всё равно продолжает писать, бот всё равно отвечает AI, 
    пытаясь возвращать к теме.
    """
    chat_context = get_chat_context(update.effective_chat.id)
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    # GPT-ответ, который поддерживает беседу
    ai_reply = await invoke_gpt("forever", user_text, chat_context)
    # В конце добавим фразу, возвращающую к сделке
    if not ai_reply.endswith("?"):
        ai_reply += "\n\nЯкщо щось ще потрібно — я тут!"

    chat_context.add_message("bot", ai_reply)
    await send_message_with_typing(update, ai_reply)
    # Возвращаемся в STAGE_ENDLESS, не завершая
    return STAGE_ENDLESS

#
# ---------------- FALLBACK (если не подходит под другие) ----------------
#
async def handle_any_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Универсальный fallback, чтобы бот отвечал на всё «очень умно» и 
    продолжал в том этапе, где находится.
    """
    chat_id = update.effective_chat.id
    chat_context = get_chat_context(chat_id)

    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    chat_context.add_message("user", user_text, sentiment)

    # Вызов GPT
    reply = await invoke_gpt(f"fallback_{chat_context.current_stage}", user_text, chat_context)
    # Добавим финальный вопрос
    if not reply.endswith("?"):
        reply += "\n\nЧи можу я допомогти з чимось ще?"

    chat_context.add_message("bot", reply)
    await send_message_with_typing(update, reply)

    # Оставляем stage без изменений
    return chat_context.current_stage

#
# ------------------- УСТАНОВКА HANDLERS -------------------
#
def setup_handlers(application: Application):
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start_command)],
        states={
            STAGE_NEEDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, needs_handler)],
            STAGE_PRESENTATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, presentation_handler)],
            STAGE_ADDITIONAL_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, additional_questions_handler)],
            STAGE_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler)],
            STAGE_CLOSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, close_handler)],
            STAGE_ENDLESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, endless_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True  # Разрешаем «переходить» в этапы, которые уже были
    )

    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_any_message))

#
# --------------- НАСТРОЙКА WEBHOOK / FLASK ----------------
#
async def setup_webhook(url: str, application: Application):
    webhook_url = f"{url}/webhook"
    await application.bot.set_webhook(webhook_url)
    logger.info(f"Webhook встановлено на: {webhook_url}")

app = Flask(__name__)

@app.route("/")
def index():
    return "Бот працює!"

@app.route("/webhook", methods=["POST"])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)
        if bot_loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
            logger.info("Webhook отримано")
        else:
            logger.error("Цикл подій не ініціалізовано.")
    return "OK"

#
# ------------------- RUN_BOT + FLASK --------------------
#
async def run_bot():
    global application, bot_loop

    if is_bot_already_running():
        logger.error("Інша інстанція бота вже запущена. Вихід.")
        sys.exit(1)

    request = HTTPXRequest(connect_timeout=20, read_timeout=40)
    application_builder = Application.builder().token(BOT_TOKEN).request(request)
    application = application_builder.build()

    setup_handlers(application)
    await setup_webhook(WEBHOOK_URL, application)

    await application.initialize()
    await application.start()

    bot_loop = asyncio.get_running_loop()
    logger.info("Бот запущено та готовий до роботи.")

def start_flask():
    port = int(os.environ.get("PORT", 10000))
    logger.info(f"Запускаємо Flask на порті {port}")
    app.run(host="0.0.0.0", port=port)

if __name__ == "__main__":
    bot_thread = threading.Thread(
        target=lambda: asyncio.run(run_bot()),
        daemon=True
    )
    bot_thread.start()
    start_flask()
