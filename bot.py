import os
import logging
import sys
import psutil
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.constants import ChatAction  # <-- исправленный импорт
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
from datetime import timezone, timedelta
from flask import Flask, request
import asyncio
import threading
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

#
# --- ЛОГИРОВАНИЕ И НАСТРОЙКИ ---
#
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", 'https://your-app.onrender.com')

openai.api_key = OPENAI_API_KEY

(
    STAGE_INTRO,             
    STAGE_NEEDS,             
    STAGE_PRESENTATION,      
    STAGE_ADDITIONAL_QUESTIONS,
    STAGE_FEEDBACK,          
    STAGE_CLOSE,             
    STAGE_FINISH
) = range(7)

bot_loop = None

#
# --- ПРОВЕРКА, НЕ ЗАПУЩЕН ЛИ БОТ УЖЕ ---
#
def is_bot_already_running():
    current_process = psutil.Process()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if process.info['name'] == current_process.name() and \
           process.info['cmdline'] == current_process.cmdline() and \
           process.info['pid'] != current_process.pid:
                return True
    return False

#
# --- ИНИЦИАЛИЗАЦИЯ VADER ---
#
logger.info("Ініціалізація VADER Sentiment Analyzer...")
sentiment_analyzer = SentimentIntensityAnalyzer()
logger.info("VADER Sentiment Analyzer ініціалізований.")

async def analyze_sentiment(text: str) -> str:
    try:
        scores = sentiment_analyzer.polarity_scores(text)
        compound = scores['compound']
        if compound >= 0.05:
            return "позитивний"
        elif compound <= -0.05:
            return "негативний"
        else:
            return "нейтральний"
    except Exception as e:
        logger.error(f"Помилка під час аналізу тональності: {e}")
        return "нейтральний"

#
# --- РАСПОЗНАВАНИЕ СОГЛАСИЯ / НЕГАТИВА ---
#
def is_affirmative(user_text: str) -> bool:
    user_text_lower = user_text.lower()
    affirmatives = [
        "так", "да", "ок", "окей", "хочу", "хотим", "продолжай", "продовжуй",
        "yes", "yeah", "yep", "yah", "si", "sí", "oui", "ja", "давай",
        "добре", "good", "ok", "sure"
    ]
    for word in affirmatives:
        if word in user_text_lower:
            return True
    return False

def is_negative(user_text: str) -> bool:
    user_text_lower = user_text.lower()
    negatives = [
        "нет", "ні", "не хочу", "no", "nope", "не треба",
        "не надо", "not now", "не готов", "не готова"
    ]
    for word in negatives:
        if word in user_text_lower:
            return True
    return False

#
# --- ВЗАИМОДЕЙСТВИЕ С GPT ---
#
async def invoke_gpt(stage: str, user_text: str, context_data: dict) -> str:
    sentiment = context_data.get("sentiment", "нейтральний")
    if sentiment == "негативний":
        empathy = "Будь ласка, прояви більше емпатії та підтримки у відповіді."
    elif sentiment == "позитивний":
        empathy = "Відповідь повинна бути дружньою та позитивною."
    else:
        empathy = "Відповідь повинна бути професійною та нейтральною."

    system_prompt = f"""
    Ти — команда експертів: SalesGuru, ObjectionsPsychologist, MarketingHacker.
    Урахуй, що наш клієнт — мама 28-45 років, шукає безпечний і комфортний тур 
    до зоопарку Ньїредьгаза для дитини.
    Ми використовуємо м'який тон, робимо акценти на сімейному відпочинку, безпеці та радості для дітей.
    Застосовуй FOMO (обмеження місць), соціальні докази, якір цін.
    Етап діалогу: {stage}.
    Повідомлення від клієнта: {user_text}.
    {empathy}
    Відповідай українською, як реальний менеджер-людина, без згадок про бота.
    Коротко та по суті.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Дотримуйся вказівок та відповідай конкретно."}
    ]
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=800,
            temperature=0.7
        )
        advice_text = response["choices"][0]["message"]["content"]
        return advice_text.strip()
    except Exception as e:
        logger.error(f"Помилка при зверненні до OpenAI: {e}")
        return "Вибачте, поки що не можу відповісти. Спробуйте пізніше."

#
# --- ИМИТАЦИЯ ПЕЧАТИ (ChatAction.TYPING) ---
#
async def typing_simulation(update: Update, text_to_send: str):
    # Показываем "typing" (менеджер печатает)
    await update.effective_chat.send_action(ChatAction.TYPING)
    delay = min(5, max(1, len(text_to_send) // 50))
    await asyncio.sleep(delay)
    # Отправляем само сообщение
    await update.message.reply_text(text_to_send)

def mention_user(update: Update) -> str:
    user = update.effective_user
    if user and user.first_name:
        return user.first_name
    return "друже"

#
# --- ЭТАП 1: ЗНАКОМСТВО ---
#
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = mention_user(update)
    _ = await invoke_gpt("intro", "/start", context.user_data)

    text = (
        f"Добрий день, {user_name}. Я Марія, ваш менеджер з туристичних пропозицій. "
        "Дозвольте поставити кілька уточнювальних питань, щоб краще зрозуміти ваші потреби?"
    )
    await typing_simulation(update, text)
    return STAGE_INTRO

async def intro_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_affirmative(user_text):
        reply_keyboard = [['Одноденний тур', 'Довгий тур']]
        markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)

        text = "Чудово! Який формат вас цікавить: одноденний тур чи більш тривалий?"
        await typing_simulation(update, text)
        return STAGE_NEEDS
    else:
        text = "Зрозуміло, тоді не буду вас турбувати. Гарного дня!"
        await typing_simulation(update, text)
        return ConversationHandler.END

#
# --- ЭТАП 2: ВЫЯВЛЕНИЕ ПОТРЕБНОСТЕЙ ---
#
async def needs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    context.user_data["tour_format"] = user_text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if "одноденний" in user_text:
        text = "Звідки ви плануєте виїжджати?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 1
        return STAGE_NEEDS
    elif "довгий" in user_text:
        text = (
            "Тривала подорож передбачає чимало деталей. "
            "Поділіться, будь ласка, вашим номером телефону чи email, щоб я могла з вами зв'язатися?"
        )
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 10
        return STAGE_NEEDS
    else:
        text = "Будь ласка, оберіть один із варіантів: Одноденний чи Довгий тур."
        await typing_simulation(update, text)
        return STAGE_NEEDS

async def needs_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    step = context.user_data.get("needs_step", 1)

    if step == 1:
        context.user_data["departure_city"] = user_text
        text = "Скільки людей планує їхати, і чи будуть діти?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 2
        return STAGE_NEEDS

    elif step == 2:
        context.user_data["passengers"] = user_text
        text = "На які дати ви орієнтуєтеся?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 3
        return STAGE_NEEDS

    elif step == 3:
        context.user_data["dates"] = user_text
        text = (
            "Дякую, у мене достатньо інформації, щоб запропонувати щось цікаве. "
            "Можемо переходити до презентації?"
        )
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 4
        return STAGE_NEEDS

    elif step == 4:
        if is_affirmative(user_text):
            return STAGE_PRESENTATION
        else:
            text = "Гаразд, звертайтеся, якщо передумаєте. Гарного дня!"
            await typing_simulation(update, text)
            return ConversationHandler.END

    elif step == 10:
        context.user_data["contact_info"] = user_text
        text = "Дякую! Скажіть, звідки ви плануєте виїжджати?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 11
        return STAGE_NEEDS

    elif step == 11:
        context.user_data["departure_city"] = user_text
        text = "Скільки людей планує поїхати, і чи будуть діти?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 12
        return STAGE_NEEDS

    elif step == 12:
        context.user_data["passengers"] = user_text
        text = "На які дати (або період) ви орієнтуєтеся?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 13
        return STAGE_NEEDS

    elif step == 13:
        context.user_data["dates"] = user_text
        text = "Прекрасно, тепер можу запропонувати тур. Перейдемо до презентації?"
        await typing_simulation(update, text)
        context.user_data["needs_step"] = 14
        return STAGE_NEEDS

    elif step == 14:
        if is_affirmative(user_text):
            return STAGE_PRESENTATION
        else:
            text = "Добре, тоді пишіть, коли будете готові обговорити деталі."
            await typing_simulation(update, text)
            return ConversationHandler.END

    text = "Вибачте, не зовсім зрозуміла. Повторіть, будь ласка."
    await typing_simulation(update, text)
    return STAGE_NEEDS

#
# --- ЭТАП 3: ПРЕЗЕНТАЦИЯ ---
#
async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    departure_city = context.user_data.get("departure_city", "")
    passengers = context.user_data.get("passengers", "")
    dates = context.user_data.get("dates", "")

    reflect_text = (
        f"Отже, плануєте поїздку з {departure_city}, людей: {passengers}, дати: {dates}. "
        "Розумію, що для вас важлива зручність та безпека."
    )
    text = (
        reflect_text + "\n\n"
        "Можу назвати вартість і розповісти про переваги. Цікаво?"
    )
    await typing_simulation(update, text)
    context.user_data["presentation_step"] = 1
    return STAGE_PRESENTATION

async def presentation_steps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    step = context.user_data.get("presentation_step", 1)

    if step == 1:
        if is_affirmative(user_text):
            text = (
                "Вартість для вашої компанії становить 2000 грн (проїзд, вхідні квитки, супровід). "
                "Хотіли б дізнатися, чому саме така ціна?"
            )
            await typing_simulation(update, text)
            context.user_data["presentation_step"] = 2
            return STAGE_PRESENTATION
        else:
            text = "Зрозуміла. Якщо зміните рішення — дайте знати."
            await typing_simulation(update, text)
            return ConversationHandler.END

    elif step == 2:
        if is_affirmative(user_text):
            text = (
                "У цю суму входить не тільки логістика й квитки, а й комфортна програма, "
                "підтримка 24/7, цікаві екскурсії. "
                "Є додаткові запитання?"
            )
            await typing_simulation(update, text)
            context.user_data["presentation_step"] = 3
            return STAGE_PRESENTATION
        else:
            text = (
                "Добре, не буду вас завантажувати деталями. "
                "Можливо, у вас є ще запитання щодо туру?"
            )
            await typing_simulation(update, text)
            context.user_data["presentation_step"] = 3
            return STAGE_PRESENTATION

    elif step == 3:
        if is_affirmative(user_text):
            return STAGE_ADDITIONAL_QUESTIONS
        else:
            return STAGE_FEEDBACK

    text = "Вибачте, не розчула. Повторіть, будь ласка."
    await typing_simulation(update, text)
    return STAGE_PRESENTATION

#
# --- ДОПОЛНИТЕЛЬНЫЕ ВОПРОСЫ ---
#
async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
    text = gpt_answer + "\n\nМожливо, є ще якісь запитання?"
    await typing_simulation(update, text)
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_negative(user_text):
        text = "Добре, тоді скажіть, як вам ця пропозиція загалом?"
        await typing_simulation(update, text)
        return STAGE_FEEDBACK
    else:
        gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
        text = gpt_answer + "\n\nЧи є ще питання?"
        await typing_simulation(update, text)
        return STAGE_ADDITIONAL_QUESTIONS

#
# --- ЭТАП ОБРАТНОЙ СВЯЗИ ---
#
async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_affirmative(user_text):
        text = (
            "Чудово! Можемо переходити до оформлення і оплати. Готові?"
        )
        await typing_simulation(update, text)
        return STAGE_CLOSE
    else:
        text = (
            "Розумію. Якщо потрібно більше часу — будь ласка. "
            "Можемо обговорити деталі або передам вас колезі. Як вчинемо?"
        )
        await typing_simulation(update, text)
        return STAGE_CLOSE

#
# --- ЭТАП ЗАКРЫТИЯ СДЕЛКИ ---
#
async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_affirmative(user_text):
        text = (
            "Ось наші реквізити:\n"
            "Картка: 0000 0000 0000 0000 (Отримувач: Family Place)\n\n"
            "Після оплати обов'язково напишіть, щоб я підтвердила бронювання!"
        )
        await typing_simulation(update, text)
        return STAGE_FINISH
    elif "менеджер" in user_text.lower() or "колега" in user_text.lower() or "людина" in user_text.lower():
        text = "Добре, передаю ваші контакти колезі. Гарного дня!"
        await typing_simulation(update, text)
        return STAGE_FINISH
    else:
        text = "Зрозуміла. Якщо з'являться питання — звертайтеся будь-коли!"
        await typing_simulation(update, text)
        return STAGE_FINISH

#
# --- ФИНАЛ ---
#
async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Дякую за звернення! Я на зв'язку, тож пишіть у будь-який час."
    await typing_simulation(update, text)
    return ConversationHandler.END

#
# --- CANCEL ---
#
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    logger.info("User %s перервав розмову /cancel.", user.first_name)
    text = "Добре, припиняємо. Якщо захочете повернутися — напишіть /start."
    await typing_simulation(update, text)
    return ConversationHandler.END

#
# --- FLASK-ПРИЛОЖЕНИЕ ---
#
app = Flask(__name__)

@app.route('/')
def index():
    return "Сервер працює!"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)
        if bot_loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
            logger.info("Webhook отримано. Менеджер друкує...")
        else:
            logger.error("Цикл подій не ініціалізовано.")
    return "OK"

async def setup_webhook(url, application):
    webhook_url = f"{url}/webhook"
    await application.bot.set_webhook(webhook_url, read_timeout=40)
    logger.info(f"Webhook встановлено на: {webhook_url}")

async def run_bot():
    global application, bot_loop
    if is_bot_already_running():
        logger.error("Інша інстанція вже запущена. Вихід.")
        sys.exit(1)

    tz = timezone(timedelta(hours=2))
    logger.info(f"Використаний часовий пояс: {tz}")

    request = HTTPXRequest(connect_timeout=20, read_timeout=40)
    application_builder = Application.builder().token(BOT_TOKEN).request(request)
    global application
    application = application_builder.build()

    application.bot_data["timezone"] = tz

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_INTRO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, intro_handler)
            ],
            STAGE_NEEDS: [
                MessageHandler(filters.Regex('^(Одноденний тур|Довгий тур)$'), needs_handler),
                MessageHandler(filters.TEXT & ~filters.COMMAND, needs_questions_handler)
            ],
            STAGE_PRESENTATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, presentation_steps_handler),
            ],
            STAGE_ADDITIONAL_QUESTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, additional_questions_loop),
            ],
            STAGE_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler),
            ],
            STAGE_CLOSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, close_handler),
            ],
            STAGE_FINISH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, finish_handler),
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )
    application.add_handler(conv_handler)

    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()

    bot_loop = asyncio.get_running_loop()
    logger.info("Менеджер онлайн і готовий обробляти повідомлення.")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Запускаємо Flask на порті {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Запущено менеджера у окремому потоці.")

    start_flask()
