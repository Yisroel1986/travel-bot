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
from datetime import timezone, timedelta
from flask import Flask, request
import asyncio
import threading
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Импортируем GoogleTranslator из deep_translator
from deep_translator import GoogleTranslator

#
# --- LOGGING AND SETTINGS ---
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
    STAGE_CITY,
    STAGE_TRAVELERS,
    STAGE_CHILD_AGE,
    STAGE_PRESENTATION,      
    STAGE_ADDITIONAL_QUESTIONS,
    STAGE_FEEDBACK,          
    STAGE_CLOSE,             
    STAGE_FINISH
) = range(10)

bot_loop = None

#
# --- CHECK IF BOT IS ALREADY RUNNING ---
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
# --- VADER INITIALIZATION ---
#
logger.info("Initializing VADER Sentiment Analyzer...")
sentiment_analyzer = SentimentIntensityAnalyzer()
logger.info("VADER Sentiment Analyzer initialized.")

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
        logger.error(f"Error during sentiment analysis: {e}")
        return "нейтральний"

#
# --- GPT INTERACTION ---
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
        {"role": "user", "content": user_text}
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
        logger.error(f"Error with OpenAI: {e}")
        return "Вибачте, поки що не можу відповісти. Спробуйте пізніше."

#
# --- RESPONSE RECOGNITION ---
#
def is_affirmative(user_text: str) -> bool:
    user_text_lower = user_text.lower()
    affirmatives = [
        "так", "да", "ок", "окей", "хочу", "хотим", "продолжай", "продовжуй",
        "yes", "yeah", "yep", "yah", "si", "sí", "oui", "ja", "давай",
        "добре", "good", "ok", "sure", "можна", "можемо", "конечно", "ага",
        "звичайно", "авжеж", "згоден", "згодна", "цікавить", "звісно"
    ]
    return any(word in user_text_lower for word in affirmatives)

def is_negative(user_text: str) -> bool:
    user_text_lower = user_text.lower()
    negatives = [
        "нет", "ні", "не хочу", "no", "nope", "не треба",
        "не надо", "not now", "не готов", "не готова", "cancel",
        "відміна", "скасувати", "пізніше", "не цікавить"
    ]
    return any(word in user_text_lower for word in negatives)

def is_neutral_greeting(user_text: str) -> bool:
    user_text_lower = user_text.lower()
    greetings = [
        "привіт", "вітаю", "доброго дня", "добрий день", "здравствуйте", 
        "hello", "hi", "хай", "thank", "спасибі", "дякую", "як справи"
    ]
    return any(word in user_text_lower for word in greetings)

async def typing_simulation(update: Update, text: str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    delay = min(5, max(1, len(text) // 50))
    await asyncio.sleep(delay)
    await update.message.reply_text(text)

def mention_user(update: Update) -> str:
    user = update.effective_user
    return user.first_name if user and user.first_name else "друже"

#
# --- CONVERSATION HANDLERS ---
#

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = mention_user(update)
    text = (
        f"Вітання, {user_name}! Я Марія, ваш тур-менеджер. "
        "Дякую, що зацікавились нашою сімейною поїздкою до зоопарку Ньїредьгаза. "
        "\n\nЦе ідеальний спосіб подарувати дитині казку, а собі — відпочинок без зайвих турбот. "
        "\n\nЧи можу я поставити кілька уточнюючих питань, щоб ми підібрали найкращий варіант?"
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
        text = "Чудово! Який тип туру вас цікавить?"
        await update.message.reply_text(text, reply_markup=markup)
        return STAGE_NEEDS
    elif is_negative(user_text):
        text = "Зрозуміло. Якщо передумаєте - пишіть. Гарного дня!"
        await typing_simulation(update, text)
        return ConversationHandler.END
    else:
        text = "Вибачте, я не зовсім зрозуміла. Можемо обговорити деталі туру?"
        await typing_simulation(update, text)
        return STAGE_INTRO

async def needs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    context.user_data["tour_type"] = user_text
    
    text = "З якого міста Ви плануєте подорож?"
    await typing_simulation(update, text)
    return STAGE_CITY

async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["departure_city"] = user_text
    
    text = "Для кого плануєте подорож?"
    await typing_simulation(update, text)
    return STAGE_TRAVELERS

async def travelers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["travelers"] = user_text
    
    text = "Скільки років дитині?"
    await typing_simulation(update, text)
    return STAGE_CHILD_AGE

async def child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["child_age"] = user_text

    tour_type = context.user_data.get("tour_type", "")
    if "довг" in tour_type:
        text = (
            "Дякую за інформацію! Для довгого туру нам потрібні ваші контактні дані. "
            "Залиште, будь ласка, номер телефону або email."
        )
    else:
        text = (
            "Чудово! У мене достатньо інформації, щоб запропонувати найкращий варіант туру. "
            "Можемо перейти до презентації?"
        )
    
    await typing_simulation(update, text)
    return STAGE_PRESENTATION

async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if "довг" in context.user_data.get("tour_type", ""):
        context.user_data["contact_info"] = user_text

    city = context.user_data.get("departure_city", "")
    travelers = context.user_data.get("travelers", "")
    child_age = context.user_data.get("child_age", "")

    text = (
        f"Чудово! Отже, плануєте поїздку з {city}, "
        f"група: {travelers}, вік дитини: {child_age} років. "
        "Розумію, що для вас важлива зручність та безпека подорожі.\n\n"
        "Можу розповісти про вартість і всі переваги туру. Цікаво?"
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
            tour_type = context.user_data.get("tour_type", "")
            if "довг" in tour_type:
                price = "4500"
            else:
                price = "2000"
            
            text = (
                f"Вартість туру для вашої групи становить {price} грн "
                "(включає проїзд, вхідні квитки та супровід). "
                "Хочете дізнатися, що входить у вартість?"
            )
            await typing_simulation(update, text)
            context.user_data["presentation_step"] = 2
            return STAGE_PRESENTATION
        
        elif is_negative(user_text):
            text = "Зрозуміла. Якщо з'являться питання — звертайтеся."
            await typing_simulation(update, text)
            return ConversationHandler.END
        
        else:
            text = "Вибачте, не зрозуміла. Хочете дізнатися про вартість туру?"
            await typing_simulation(update, text)
            return STAGE_PRESENTATION

    elif step == 2:
        if is_affirmative(user_text):
            text = (
                "У вартість входить:\n"
                "- Комфортний транспорт\n"
                "- Вхідні квитки до зоопарку\n"
                "- Супровід гіда\n"
                "- Страхування\n"
                "- Підтримка 24/7\n\n"
                "Маєте запитання щодо програми туру?"
            )
            await typing_simulation(update, text)
            context.user_data["presentation_step"] = 3
            return STAGE_PRESENTATION
        
        elif is_negative(user_text):
            text = "Можливо, у вас є інші запитання щодо туру?"
            await typing_simulation(update, text)
            context.user_data["presentation_step"] = 3
            return STAGE_PRESENTATION
        
        else:
            text = "Вибачте, я не зрозуміла. Бажаєте дізнатись деталі програми?"
            await typing_simulation(update, text)
            return STAGE_PRESENTATION

    elif step == 3:
        if is_affirmative(user_text):
            return STAGE_ADDITIONAL_QUESTIONS
        elif is_negative(user_text):
            return STAGE_FEEDBACK
        else:
            text = "У вас є додаткові запитання щодо туру?"
            await typing_simulation(update, text)
            return STAGE_PRESENTATION

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    # Заменяем вызов googletrans на deep-translator, если необходимо
    # Например, перевод входящего сообщения на нужный язык перед отправкой GPT
    # Здесь показан пример использования GPT, так что возможно, вам не нужен перевод

    gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
    text = gpt_answer + "\n\nЧи є ще запитання?"
    await typing_simulation(update, text)
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_negative(user_text):
        text = "Добре, як вам така пропозиція загалом?"
        await typing_simulation(update, text)
        return STAGE_FEEDBACK
    else:
        gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
        text = gpt_answer + "\n\nМожливо, є ще питання?"
        await typing_simulation(update, text)
        return STAGE_ADDITIONAL_QUESTIONS

async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_affirmative(user_text):
        text = "Чудово! Можемо переходити до оформлення? Потрібно буде внести передоплату для бронювання місць."
        await typing_simulation(update, text)
        return STAGE_CLOSE
    elif is_negative(user_text):
        text = (
            "Розумію ваші сумніви. Можемо обговорити те, що вас бентежить, "
            "або я можу запропонувати альтернативні варіанти. Що оберете?"
        )
        await typing_simulation(update, text)
        return STAGE_CLOSE
    else:
        text = "Вибачте, не зрозуміла вашу відповідь. Пропозиція вам підходить?"
        await typing_simulation(update, text)
        return STAGE_FEEDBACK

async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_affirmative(user_text):
        text = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Після оплати надішліть, будь ласка, скріншот чеку, "
            "і я відразу відправлю вам підтвердження бронювання!"
        )
        await typing_simulation(update, text)
        return STAGE_FINISH
    elif "альтернатив" in user_text.lower() or "інш" in user_text.lower():
        text = (
            "Звичайно, у нас є інші варіанти турів. "
            "Можу запропонувати тур в інші дати або з іншою програмою. "
            "Що вас більше цікавить?"
        )
        await typing_simulation(update, text)
        return STAGE_CLOSE
    elif is_negative(user_text):
        text = "Дякую за інтерес! Якщо передумаєте - пишіть, завжди рада допомогти."
        await typing_simulation(update, text)
        return STAGE_FINISH
    else:
        text = "Перепрошую, не зрозуміла. Ви готові перейти до оформлення туру?"
        await typing_simulation(update, text)
        return STAGE_CLOSE

async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Дякую за звернення! Я завжди на зв'язку, "
        "тож якщо виникнуть питання - пишіть у будь-який час."
    )
    await typing_simulation(update, text)
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.message.from_user
    logger.info("User %s cancelled the conversation.", user.first_name)
    text = "Добре, завершуємо. Якщо захочете повернутися до розмови - напишіть /start"
    await typing_simulation(update, text)
    return ConversationHandler.END

#
# --- FLASK APP ---
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
            logger.info("Webhook received. Processing update...")
        else:
            logger.error("Event loop not initialized.")
    return "OK"

async def setup_webhook(url, application):
    webhook_url = f"{url}/webhook"
    await application.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to: {webhook_url}")

async def run_bot():
    global application, bot_loop
    if is_bot_already_running():
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    tz = timezone(timedelta(hours=2))
    logger.info(f"Using timezone: {tz}")

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
                MessageHandler(filters.TEXT & ~filters.COMMAND, needs_handler)
            ],
            STAGE_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, city_handler)
            ],
            STAGE_TRAVELERS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, travelers_handler)
            ],
            STAGE_CHILD_AGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, child_age_handler)
            ],
            STAGE_PRESENTATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, presentation_steps_handler)
            ],
            STAGE_ADDITIONAL_QUESTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, additional_questions_loop)
            ],
            STAGE_FEEDBACK: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler)
            ],
            STAGE_CLOSE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, close_handler)
            ],
            STAGE_FINISH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, finish_handler)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(conv_handler)

    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()

    bot_loop = asyncio.get_running_loop()
    logger.info("Bot manager is online and ready to process messages.")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Bot manager started in separate thread.")
    start_flask()
