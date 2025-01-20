import os
import logging
import sys
import psutil
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest  # Для расширенных таймаутов

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

# ЭТАПЫ (по сценарию)
(
    STAGE_INTRO,             # 1. Знакомство
    STAGE_NEEDS,             # 2. Выявление потребностей
    STAGE_PRESENTATION,      # 3. Презентация
    STAGE_ADDITIONAL_QUESTIONS,
    STAGE_FEEDBACK,          # Обратная связь
    STAGE_CLOSE,             # 4. Закрытие сделки
    STAGE_FINISH
) = range(7)

# Глобальная переменная для цикла событий
bot_loop = None

def is_bot_already_running():
    """
    Проверка, не запущено ли второе копии приложения.
    """
    current_process = psutil.Process()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if process.info['name'] == current_process.name() and \
           process.info['cmdline'] == current_process.cmdline() and \
           process.info['pid'] != current_process.pid:
                return True
    return False

# Инициализация VADER
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

async def invoke_gpt(stage: str, user_text: str, context_data: dict) -> str:
    """
    Вызывает ChatCompletion GPT, учитывая текущий этап и тональность.
    Возвращает текст ответа.
    """
    sentiment = context_data.get("sentiment", "нейтральний")
    if sentiment == "негативний":
        empathy = "Будь ласка, прояви більше емпатії та підтримки у відповіді."
    elif sentiment == "позитивний":
        empathy = "Відповідь повинна бути дружньою та позитивною."
    else:
        empathy = "Відповідь повинна бути професійною та нейтральною."

    system_prompt = f"""
    Ти — команда експертів: SalesGuru, ObjectionsPsychologist, MarketingHacker.
    Урахуй, що наш цільовий клієнт — мама 28-45 років, цінує сім'ю, шукає безпечний і 
    комфортний тур до зоопарку Ньїредьгаза для дитини. 
    Ми використовуємо жіночий м'який тон, робимо акценти на відпочинку для мами, 
    на дитячій радості, безпеці. 
    Застосовуй FOMO (обмеження місць), соціальні докази, якір цін (інші тури дорожчі, але ми даємо те саме).
    Етап діалогу: {stage}.
    Повідомлення від клієнта: {user_text}.
    {empathy}
    Відповідай українською, як менеджер-людина, без згадок, що ти чат-бот.
    Коротко, по суті, не перевантажуй зайвим.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Будь ласка, дотримуйся інструкцій."}
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
        return "Вибачте, наразі не можу відповісти. Спробуйте пізніше."

def mention_user(update: Update) -> str:
    user = update.effective_user
    if user and user.first_name:
        return user.first_name
    return "друже"

#
# Этап 1: Знакомство
#
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = mention_user(update)
    _ = await invoke_gpt("intro", "/start", context.user_data)

    text = (
        f"Добрий день, {user_name}. Я Марія, ваш менеджер з туристичних пропозицій. "
        "Дозвольте поставити кілька уточнювальних питань, щоб краще зрозуміти ваші потреби?"
    )
    await update.message.reply_text(text)
    return STAGE_INTRO

async def intro_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if any(x in user_text for x in ["так", "да", "ок", "добре", "хочу"]):
        reply_keyboard = [['Одноденний тур', 'Довгий тур']]
        markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)

        await update.message.reply_text(
            "Чудово! Який формат вас цікавить: одноденний тур чи більш тривалий?",
            reply_markup=markup
        )
        return STAGE_NEEDS
    else:
        await update.message.reply_text(
            "Зрозуміло, тоді не буду вас турбувати. Гарного дня!",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

#
# Этап 2: Выявление потребностей
#
async def needs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    context.user_data["tour_format"] = user_text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    # Если одноденний тур => задаём уточняющие вопросы (город, люди, даты)
    if "одноденний" in user_text:
        await update.message.reply_text(
            "Звідки ви плануєте виїжджати?",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["needs_step"] = 1
        return STAGE_NEEDS
    elif "довгий" in user_text:
        # Для длинных туров спросим контактные данные заранее
        await update.message.reply_text(
            "Тривала подорож передбачає чимало деталей. "
            "Поділіться, будь ласка, вашим номером телефону чи email, щоб я могла з вами зв'язатися?",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["needs_step"] = 10
        return STAGE_NEEDS
    else:
        await update.message.reply_text("Будь ласка, оберіть один із варіантів: Одноденний чи Довгий тур.")
        return STAGE_NEEDS

async def needs_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    step = context.user_data.get("needs_step", 1)
    if step == 1:
        context.user_data["departure_city"] = user_text
        await update.message.reply_text("Скільки людей планує їхати, і чи будуть діти?")
        context.user_data["needs_step"] = 2
        return STAGE_NEEDS
    elif step == 2:
        context.user_data["passengers"] = user_text
        await update.message.reply_text("На які дати ви орієнтуєтеся?")
        context.user_data["needs_step"] = 3
        return STAGE_NEEDS
    elif step == 3:
        context.user_data["dates"] = user_text
        await update.message.reply_text(
            "Дякую, у мене достатньо інформації, щоб запропонувати щось цікаве. "
            "Можемо переходити до презентації?"
        )
        context.user_data["needs_step"] = 4
        return STAGE_NEEDS
    elif step == 4:
        # Проверяем согласие
        user_text_lower = user_text.lower()
        if any(x in user_text_lower for x in ["так", "да", "ок", "добре", "хочу"]):
            return STAGE_PRESENTATION
        else:
            await update.message.reply_text("Гаразд, звертайтеся, якщо передумаєте. Гарного дня!")
            return ConversationHandler.END

    elif step == 10:
        context.user_data["contact_info"] = user_text
        await update.message.reply_text("Дякую! Скажіть, звідки ви плануєте виїжджати?")
        context.user_data["needs_step"] = 11
        return STAGE_NEEDS
    elif step == 11:
        context.user_data["departure_city"] = user_text
        await update.message.reply_text("Скільки людей планує поїхати, і чи будуть діти?")
        context.user_data["needs_step"] = 12
        return STAGE_NEEDS
    elif step == 12:
        context.user_data["passengers"] = user_text
        await update.message.reply_text("На які дати (або період) ви орієнтуєтеся?")
        context.user_data["needs_step"] = 13
        return STAGE_NEEDS
    elif step == 13:
        context.user_data["dates"] = user_text
        await update.message.reply_text(
            "Прекрасно, тепер можу запропонувати тур. Перейдемо до презентації?"
        )
        context.user_data["needs_step"] = 14
        return STAGE_NEEDS
    elif step == 14:
        user_text_lower = user_text.lower()
        if any(x in user_text_lower for x in ["так", "да", "ок", "добре", "хочу"]):
            return STAGE_PRESENTATION
        else:
            await update.message.reply_text("Добре, тоді пишіть, коли будете готові обговорити деталі.")
            return ConversationHandler.END

    # Неизвестный шаг
    await update.message.reply_text("Вибачте, не зовсім зрозуміла. Повторіть, будь ласка.")
    return STAGE_NEEDS

#
# Этап 3: Презентация
#
async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    departure_city = context.user_data.get("departure_city", "")
    passengers = context.user_data.get("passengers", "")
    dates = context.user_data.get("dates", "")

    # «Зеркалим» потребности (боль)
    reflect_text = (
        f"Отже, плануєте поїздку з {departure_city}, людей: {passengers}, дати: {dates}. "
        "Розумію, що для вас важлива зручність та безпека."
    )

    # Сразу спросим, готовы ли услышать цену
    await update.message.reply_text(
        reflect_text + "\n\nМожу назвати вартість і розповісти про переваги. Цікаво?"
    )
    context.user_data["presentation_step"] = 1
    return STAGE_PRESENTATION

async def presentation_steps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    step = context.user_data.get("presentation_step", 1)

    if step == 1:
        if any(x in user_text for x in ["так", "да", "ок", "хочу"]):
            # Озвучиваем цену
            await update.message.reply_text(
                "Вартість для вашої компанії становить 2000 грн (проїзд, вхідні квитки, супровід). "
                "Хотіли б дізнатися, чому саме така ціна?"
            )
            context.user_data["presentation_step"] = 2
            return STAGE_PRESENTATION
        else:
            await update.message.reply_text("Зрозуміла. Якщо зміните рішення — дайте знати.")
            return ConversationHandler.END
    elif step == 2:
        if any(x in user_text for x in ["так", "да", "ок", "хочу"]):
            await update.message.reply_text(
                "У цю суму входить не тільки логістика й квитки, а й комфортна програма, "
                "підтримка 24/7, цікаві екскурсії. "
                "Є додаткові запитання?"
            )
            context.user_data["presentation_step"] = 3
            return STAGE_PRESENTATION
        else:
            await update.message.reply_text(
                "Добре, не буду вас завантажувати деталями. "
                "Можливо, у вас є ще запитання щодо туру?"
            )
            context.user_data["presentation_step"] = 3
            return STAGE_PRESENTATION
    elif step == 3:
        # Если пользователь говорит "так, є питання", уйдём в доп. вопросы
        if any(x in user_text for x in ["так", "да", "ок", "хочу", "ще", "есть", "є"]):
            return STAGE_ADDITIONAL_QUESTIONS
        else:
            # Иначе переходим к этапу обратной связи
            return STAGE_FEEDBACK

    await update.message.reply_text("Вибачте, не розчула. Повторіть, будь ласка.")
    return STAGE_PRESENTATION

#
# Дополнительные вопросы
#
async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    # Отвечаем коротко через GPT
    gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
    await update.message.reply_text(
        gpt_answer + "\n\nМожливо, є ще якісь запитання?"
    )
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if any(x in user_text for x in ["ні", "нет", "немає", "все", "достатньо"]):
        # Переходим к обратной связи
        await update.message.reply_text("Добре, тоді скажіть, як вам ця пропозиція загалом?")
        return STAGE_FEEDBACK
    else:
        # Снова отвечаем
        gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
        await update.message.reply_text(
            gpt_answer + "\n\nЧи є ще питання?"
        )
        return STAGE_ADDITIONAL_QUESTIONS

#
# Этап обратной связи
#
async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    # Здесь оцениваем, готов ли клиент
    if any(x in user_text.lower() for x in ["подобається", "нрав", "цікаво", "хочу", "купую", "готов"]):
        await update.message.reply_text(
            "Чудово! Можемо переходити до оформлення і оплати. Готові?"
        )
        return STAGE_CLOSE
    else:
        # Предлагаем связаться с менеджером или взять паузу
        await update.message.reply_text(
            "Розумію. Якщо потрібно більше часу — будь ласка. "
            "Можемо обговорити додаткові деталі, або за бажанням передам вас колезі. "
            "Як вчинемо?"
        )
        return STAGE_CLOSE

#
# Этап 4: Закрытие сделки
#
async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if any(x in user_text for x in ["так", "оплатити", "готов", "оплата", "давай"]):
        await update.message.reply_text(
            "Ось наші реквізити:\n"
            "Картка: 0000 0000 0000 0000 (Отримувач: Family Place)\n\n"
            "Після оплати обов'язково повідомте, щоб я підтвердила бронювання!"
        )
        return STAGE_FINISH
    elif any(x in user_text for x in ["менеджер", "людина", "колега"]):
        await update.message.reply_text(
            "Добре, передаю ваші контакти колезі, він з вами зв'яжеться. Гарного дня!",
            reply_markup=ReplyKeyboardRemove()
        )
        return STAGE_FINISH
    else:
        await update.message.reply_text(
            "Зрозуміла. Якщо з'являться додаткові питання — звертайтесь! Гарного дня!",
            reply_markup=ReplyKeyboardRemove()
        )
        return STAGE_FINISH

#
# Финал
#
async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Дякую за звернення! Я на зв'язку, тож пишіть будь-коли.",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    logger.info("User %s перервав розмову /cancel.", user.first_name)
    await update.message.reply_text(
        "Добре, припиняємо. Якщо захочете повернутися до обговорення — пишіть!",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

#
# Flask-приложение
#
app = Flask(__name__)

@app.route('/')
def index():
    # Чтобы пользователь, зайдя по корневому URL, не видел "бот".
    return "Сервер працює!"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)
        if bot_loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
            logger.info("Webhook отримано та передано менеджеру.")
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

    tz = timezone(timedelta(hours=2))  # UTC+2
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
