import os
import logging
import sys
import psutil
import sqlite3
import json
from dotenv import load_dotenv

from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    JobQueue
)
from telegram.request import HTTPXRequest

import openai
from datetime import timezone, timedelta, datetime
from flask import Flask, request
import asyncio
import threading
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# ИМПОРТ (если нужно) из deep_translator
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
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", 'https://travel-bot-5u6d.onrender.com')

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

# --- Настройка задержки для follow-up
FOLLOWUP_DELAY_SECONDS = 120  # например, 2 минуты

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


#
# --- HELPER FUNCTIONS ---
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

async def analyze_sentiment(text: str) -> str:
    """Определяем общий тон (позитив, негатив, нейтрал)."""
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

def detect_fear_keywords(text: str) -> bool:
    """Проверяем, нет ли слов, указывающих на сильное беспокойство/страх."""
    fear_related = ["боюся", "переживаю", "страх", "хвилюю", "небезпечно", "тривожить", "турбую"]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in fear_related)

def detect_price_keywords(text: str) -> bool:
    """Проверяем упоминания о завышенной цене."""
    price_related = [
        "дорого", "висока ціна", "too expensive", 
        "costly", "завелика ціна", "слишком дорого"
    ]
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in price_related)

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

    if detect_fear_keywords(user_text):
        empathy += (
            " Клієнт хвилюється щодо безпеки. "
            "Опиши докладно заходи безпеки й страхування, заспокой його."
        )

    if detect_price_keywords(user_text):
        empathy += (
            " Клієнт вважає, що ціна зависока. "
            "Поясни, чому це вигідна пропозиція, звернись до якірної ціни, "
            "соціальних доказів, бонусів тощо."
        )

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
# --- TYPING LOGIC ---
#
def adaptive_typing_delay(text: str, sentiment: str) -> float:
    base_factor = len(text) / 50  
    if sentiment == "негативний":
        delay = 1.0 + base_factor * 0.4
    elif sentiment == "позитивний":
        delay = 2.0 + base_factor * 0.6
    else:
        delay = 1.5 + base_factor * 0.5

    delay = max(1, min(10, delay))
    return delay

async def typing_simulation(update: Update, text: str, sentiment: str = "нейтральний"):
    await update.effective_chat.send_action(ChatAction.TYPING)
    delay = adaptive_typing_delay(text, sentiment)
    await asyncio.sleep(delay)
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

def mention_user(update: Update) -> str:
    user = update.effective_user
    return user.first_name if user and user.first_name else "друже"

#
# --- SQLITE DB ---
#
def init_db():
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_state (
            user_id TEXT PRIMARY KEY,
            current_stage INTEGER,
            user_data TEXT,
            last_interaction TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def load_user_state(user_id: str):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT current_stage, user_data FROM conversation_state WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, None

def save_user_state(user_id: str, current_stage: int, user_data: dict):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    user_data_json = json.dumps(user_data, ensure_ascii=False)
    now = datetime.now().isoformat()
    c.execute("""
        INSERT OR REPLACE INTO conversation_state (user_id, current_stage, user_data, last_interaction)
        VALUES (?, ?, ?, ?)
    """, (user_id, current_stage, user_data_json, now))
    conn.commit()
    conn.close()

#
# --- FOLLOW-UP LOGIC (PREDICTIVE MESSAGES) ---
#
FOLLOWUP_DELAY_SECONDS = 120

def followup_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = context.job.chat_id
    message = job_data.get("message", "Нагадую, що знижка діє до кінця тижня! Якщо виникли питання — пишіть!")
    context.bot.send_message(chat_id=chat_id, text=message)

def schedule_followup(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_data: dict):
    job_queue = context.job_queue
    old_job = user_data.get("followup_job")
    if old_job:
        old_job.schedule_removal()

    job = job_queue.run_once(
        followup_callback,
        FOLLOWUP_DELAY_SECONDS,
        chat_id=chat_id,
        data={"message": "Нагадую, що знижка діє до кінця тижня! Якщо виникли питання — пишіть!"}
    )
    user_data["followup_job"] = job

def cancel_followup(user_data: dict):
    old_job = user_data.get("followup_job")
    if old_job:
        old_job.schedule_removal()
        user_data["followup_job"] = None

#
# --- FLASK APP & BOT HANDLERS ---
#
app = Flask(__name__)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()

    cancel_followup(context.user_data)

    saved_stage, saved_user_data_json = load_user_state(user_id)
    if saved_stage is not None and saved_user_data_json is not None:
        context.user_data.update(json.loads(saved_user_data_json))
        text = (
            "Привіт знову! Ви маєте незавершену розмову. "
            "Бажаєте продовжити з того ж місця або почати заново?\n\n"
            "Введіть 'Продовжити' чи 'Почати знову'."
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_INTRO, context.user_data)
        schedule_followup(context, update.effective_chat.id, context.user_data)
        return STAGE_INTRO
    else:
        user_name = mention_user(update)
        text = (
            f"Привіт, {user_name}!\n"
            "Я Марія, Ваш менеджер компанії Family Place. "
            "Дозвольте поставити кілька уточнюючих питань, добре?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_INTRO, context.user_data)
        schedule_followup(context, update.effective_chat.id, context.user_data)
        return STAGE_INTRO

async def intro_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_affirmative(user_text):
        response_text = "Чудово! Який тип туру вас цікавить? (Одноденний чи довгий)"
        await typing_simulation(update, response_text, sentiment)
        save_user_state(user_id, STAGE_NEEDS, context.user_data)
        schedule_followup(context, update.effective_chat.id, context.user_data)
        return STAGE_NEEDS
    elif is_negative(user_text):
        response_text = (
            "Зрозуміло. Якщо передумаєте — пишіть. "
            "Чи можемо обговорити якісь інші деталі?"
        )
        await typing_simulation(update, response_text, sentiment)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_followup(context, update.effective_chat.id, context.user_data)
        return STAGE_ADDITIONAL_QUESTIONS
    else:
        if user_text.lower().startswith("продовжити"):
            saved_stage, saved_user_data_json = load_user_state(user_id)
            if saved_stage is not None:
                context.user_data.update(json.loads(saved_user_data_json))
                response_text = "Повертаємось до вашої попередньої розмови."
                await typing_simulation(update, response_text, sentiment)
                schedule_followup(context, update.effective_chat.id, context.user_data)
                return saved_stage
            else:
                response_text = "Немає даних. Почнемо заново. Який тип туру вас цікавить?"
                await typing_simulation(update, response_text, sentiment)
                save_user_state(user_id, STAGE_NEEDS, context.user_data)
                schedule_followup(context, update.effective_chat.id, context.user_data)
                return STAGE_NEEDS
        elif user_text.lower().startswith("почати знову"):
            context.user_data.clear()
            response_text = "Гаразд, почнімо з нуля. Який тип туру вас цікавить?"
            await typing_simulation(update, response_text, sentiment)
            save_user_state(user_id, STAGE_NEEDS, context.user_data)
            schedule_followup(context, update.effective_chat.id, context.user_data)
            return STAGE_NEEDS
        else:
            response_text = "Вибачте, я не зовсім зрозуміла. Можемо обговорити деталі туру?"
            await typing_simulation(update, response_text, sentiment)
            save_user_state(user_id, STAGE_INTRO, context.user_data)
            schedule_followup(context, update.effective_chat.id, context.user_data)
            return STAGE_INTRO

async def needs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("needs", user_text, context.user_data)
    response_text = gpt_answer + "\n\nЗ якого міста Ви плануєте подорож?"

    await typing_simulation(update, response_text, sentiment)
    context.user_data["tour_type"] = user_text.lower()
    save_user_state(user_id, STAGE_CITY, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_CITY

async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("city", user_text, context.user_data)
    response_text = gpt_answer + "\n\nДля кого плануєте подорож?"

    await typing_simulation(update, response_text, sentiment)
    context.user_data["departure_city"] = user_text
    save_user_state(user_id, STAGE_TRAVELERS, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_TRAVELERS

async def travelers_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("travelers", user_text, context.user_data)
    response_text = gpt_answer + "\n\nСкільки років дитині?"

    await typing_simulation(update, response_text, sentiment)
    context.user_data["travelers"] = user_text
    save_user_state(user_id, STAGE_CHILD_AGE, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_CHILD_AGE

async def child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("child_age", user_text, context.user_data)
    tour_type = context.user_data.get("tour_type", "")

    if "довг" in tour_type:
        script_text = (
            "Дякую за інформацію! Для довгого туру нам потрібні ваші контактні дані. "
            "Залиште, будь ласка, номер телефону або email."
        )
    else:
        script_text = (
            "Чудово! У мене достатньо інформації, щоб запропонувати найкращий варіант туру. "
            "Можемо перейти до презентації?"
        )

    response_text = gpt_answer + "\n\n" + script_text
    await typing_simulation(update, response_text, sentiment)
    context.user_data["child_age"] = user_text
    save_user_state(user_id, STAGE_PRESENTATION, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_PRESENTATION

async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    tour_type = context.user_data.get("tour_type", "")
    if "довг" in tour_type and "contact_info" not in context.user_data:
        context.user_data["contact_info"] = user_text

    gpt_answer = await invoke_gpt("presentation", user_text, context.user_data)

    city = context.user_data.get("departure_city", "")
    travelers = context.user_data.get("travelers", "")
    child_age = context.user_data.get("child_age", "")

    script_text = (
        f"Чудово! Отже, плануєте поїздку з {city}, "
        f"група: {travelers}, вік дитини: {child_age} років. "
        "Розумію, що для вас важлива зручність та безпека подорожі.\n\n"
        "Можу розповісти про вартість і всі переваги туру. Цікаво?"
    )
    response_text = gpt_answer + "\n\n" + script_text

    await typing_simulation(update, response_text, sentiment)
    context.user_data["presentation_step"] = 1
    save_user_state(user_id, STAGE_PRESENTATION, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_PRESENTATION

async def presentation_steps_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("presentation_steps", user_text, context.user_data)
    step = context.user_data.get("presentation_step", 1)

    response_text = gpt_answer

    # Крок 1
    if step == 1:
        if is_affirmative(user_text):
            tour_type = context.user_data.get("tour_type", "")
            if "довг" in tour_type:
                price = "4500"
            else:
                price = "2000"
            
            response_text += (
                f"\n\nВартість туру для вашої групи становить {price} грн "
                "(включає проїзд, вхідні квитки та супровід). "
                "Хочете дізнатися, що входить у вартість?"
            )
            context.user_data["presentation_step"] = 2
        elif is_negative(user_text):
            response_text += (
                "\n\nЗрозуміла. Якщо з'являться питання — звертайтеся. "
                "Можливо, маєте інші запитання?"
            )
            save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
            await typing_simulation(update, response_text, sentiment)
            schedule_followup(context, update.effective_chat.id, context.user_data)
            return STAGE_ADDITIONAL_QUESTIONS
        else:
            response_text += (
                "\n\nВибачте, не зрозуміла. Хочете дізнатися про вартість туру?"
            )

    elif step == 2:
        if is_affirmative(user_text):
            response_text += (
                "\n\nУ вартість входить:\n"
                "- Комфортний транспорт\n"
                "- Вхідні квитки до зоопарку\n"
                "- Супровід гіда\n"
                "- Страхування\n"
                "- Підтримка 24/7\n\n"
                "Маєте запитання щодо програми туру?"
            )
            context.user_data["presentation_step"] = 3
        elif is_negative(user_text):
            response_text += (
                "\n\nМожливо, у вас є інші запитання щодо туру?"
            )
            context.user_data["presentation_step"] = 3
        else:
            response_text += (
                "\n\nВибачте, я не зрозуміла. Бажаєте дізнатись деталі програми?"
            )

    elif step == 3:
        if is_affirmative(user_text):
            save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
            response_text += "\n\nЧудово, тоді перейдемо до наступних питань."
            await typing_simulation(update, response_text, sentiment)
            schedule_followup(context, update.effective_chat.id, context.user_data)
            return STAGE_ADDITIONAL_QUESTIONS
        elif is_negative(user_text):
            save_user_state(user_id, STAGE_FEEDBACK, context.user_data)
            response_text += "\n\nГаразд, перейдемо до обговорення ваших вражень."
            await typing_simulation(update, response_text, sentiment)
            schedule_followup(context, update.effective_chat.id, context.user_data)
            return STAGE_FEEDBACK
        else:
            response_text += "\n\nУ вас є додаткові запитання щодо туру?"

    await typing_simulation(update, response_text, sentiment)
    save_user_state(user_id, STAGE_PRESENTATION, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_PRESENTATION

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
    response_text = gpt_answer + "\n\nЧи є ще запитання?"

    await typing_simulation(update, response_text, sentiment)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    if is_negative(user_text):
        response_text = "Добре, як вам така пропозиція загалом?"
        await typing_simulation(update, response_text, sentiment)
        save_user_state(user_id, STAGE_FEEDBACK, context.user_data)
        schedule_followup(context, update.effective_chat.id, context.user_data)
        return STAGE_FEEDBACK
    else:
        gpt_answer = await invoke_gpt("additional_questions", user_text, context.user_data)
        response_text = gpt_answer + "\n\nМожливо, є ще питання?"
        await typing_simulation(update, response_text, sentiment)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_followup(context, update.effective_chat.id, context.user_data)
        return STAGE_ADDITIONAL_QUESTIONS

async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("feedback", user_text, context.user_data)
    response_text = gpt_answer

    if is_affirmative(user_text):
        response_text += (
            "\n\nЧудово! Можемо переходити до оформлення? "
            "Потрібно буде внести передоплату для бронювання місць."
        )
        next_stage = STAGE_CLOSE
    elif is_negative(user_text):
        response_text += (
            "\n\nРозумію ваші сумніви. Можемо обговорити те, що вас бентежить, "
            "або я можу запропонувати альтернативні варіанти. Що оберете?"
        )
        next_stage = STAGE_CLOSE
    else:
        response_text += "\n\nВибачте, не зрозуміла вашу відповідь. Пропозиція вам підходить?"
        next_stage = STAGE_FEEDBACK

    await typing_simulation(update, response_text, sentiment)
    save_user_state(user_id, next_stage, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return next_stage

async def close_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("close", user_text, context.user_data)
    response_text = gpt_answer

    if is_affirmative(user_text):
        response_text += (
            "\n\nЧудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Після оплати надішліть, будь ласка, скріншот чеку, "
            "і я відразу відправлю вам підтвердження бронювання!"
        )
        next_stage = STAGE_FINISH
    elif "альтернатив" in user_text.lower() or "інш" in user_text.lower():
        response_text += (
            "\n\nЗвичайно, у нас є інші варіанти турів. "
            "Можу запропонувати тур в інші дати або з іншою програмою. "
            "Що вас більше цікавить?"
        )
        next_stage = STAGE_CLOSE
    elif is_negative(user_text):
        response_text += (
            "\n\nДякую за інтерес! Якщо передумаєте — пишіть, завжди рада допомогти."
        )
        next_stage = STAGE_FINISH
    else:
        response_text += (
            "\n\nПерепрошую, не зрозуміла. Ви готові перейти до оформлення туру?"
        )
        next_stage = STAGE_CLOSE

    await typing_simulation(update, response_text, sentiment)
    save_user_state(user_id, next_stage, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return next_stage

async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_followup(context.user_data)
    user_id = str(update.effective_user.id)
    user_text = update.message.text

    sentiment = await analyze_sentiment(user_text)
    context.user_data["sentiment"] = sentiment

    gpt_answer = await invoke_gpt("finish", user_text, context.user_data)
    response_text = gpt_answer + (
        "\n\nДякую за звернення! Я завжди на зв'язку. "
        "Якщо виникнуть питання — пишіть у будь-який час.\n\n"
        "Чим ще можу допомогти?"
    )

    await typing_simulation(update, response_text, sentiment)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_ADDITIONAL_QUESTIONS

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cancel_followup(context.user_data)
    user = update.message.from_user
    logger.info("User %s tried to cancel the conversation.", user.first_name)
    sentiment = context.user_data.get("sentiment", "нейтральний")
    text = (
        "Гаразд, ми не будемо зараз завершувати. "
        "Якщо виникнуть питання — завжди можете звернутись!"
    )
    await typing_simulation(update, text, sentiment)

    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_followup(context, update.effective_chat.id, context.user_data)
    return STAGE_ADDITIONAL_QUESTIONS

#
# --- WEBHOOK & BOT LAUNCH ---
#
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
            ],
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
