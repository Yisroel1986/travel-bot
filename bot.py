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
    CallbackContext
)
from telegram.request import HTTPXRequest
from datetime import datetime
from flask import Flask, request
import asyncio
import threading
import re
import requests  # Для обращения к KeyCRM

# Попытка импорта spaCy и загрузка украинской модели
try:
    import spacy
    nlp_uk = spacy.load("uk_core_news_sm")
    logging.info("spaCy and Ukrainian model loaded successfully.")
except Exception as e:
    nlp_uk = None
    logging.warning("spaCy or Ukrainian model not available. Falling back to basic keyword analysis.")

# Попытка импорта OpenAI и настройка API-ключа
try:
    import openai
except Exception as e:
    openai = None
    logging.warning("OpenAI library not available. ChatGPT fallback disabled.")

# Попытка импорта Transformers для анализа тональности
try:
    from transformers import pipeline
    sentiment_pipeline = pipeline("sentiment-analysis", model="nlptown/bert-base-multilingual-uncased-sentiment")
    logging.info("Transformers sentiment analysis pipeline loaded successfully.")
except Exception as e:
    sentiment_pipeline = None
    logging.warning("Transformers sentiment analysis pipeline not available.")

# --- LOGGING AND SETTINGS ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CRM_API_KEY = os.getenv("CRM_API_KEY")
CRM_API_URL = os.getenv("CRM_API_URL", "https://familyplace.keycrm.app/api/v1/products")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", 'https://your-app.onrender.com')

if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# Проверка, что другие процессы бота не запущены
def is_bot_already_running():
    current_process = psutil.Process()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if (
            process.info['name'] == current_process.name()
            and process.info['cmdline'] == current_process.cmdline()
            and process.info['pid'] != current_process.pid
        ):
            return True
    return False

# --- STATE DEFINITIONS ---
(
    STAGE_GREET,
    STAGE_DEPARTURE,
    STAGE_TRAVEL_PARTY,
    STAGE_CHILD_AGE,
    STAGE_CHOICE,
    STAGE_DETAILS,
    STAGE_ADDITIONAL_QUESTIONS,
    STAGE_IMPRESSION,
    STAGE_CLOSE_DEAL,
    STAGE_PAYMENT,
    STAGE_PAYMENT_CONFIRM,
    STAGE_END
) = range(12)

NO_RESPONSE_DELAY_SECONDS = 6 * 3600  # 6 часов

# --- FLASK APP ---
app = Flask(__name__)
application = None

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
# --- CRM INTEGRATION ---
#

def fetch_all_products():
    """
    Получаем *все* продукты (туры) из KeyCRM, перебирая страницы, пока не кончатся.
    Для каждого запроса указываем limit=50 (максимум) и page=n.
    Возвращаем общий список (list) словарей.
    """
    if not CRM_API_KEY or not CRM_API_URL:
        logger.warning("CRM_API_KEY or CRM_API_URL not found. Returning empty tours list.")
        return []

    headers = {
        "Authorization": f"Bearer {CRM_API_KEY}",
        "Accept": "application/json"
    }

    all_items = []
    page = 1
    limit = 50  # максимум 50, согласно документации

    while True:
        params = {"page": page, "limit": limit}
        try:
            resp = requests.get(CRM_API_URL, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                # Ожидаем структуру:
                # {
                #   "total": 100,
                #   "current_page": 1,
                #   "per_page": 15,
                #   "data": [...],
                #   ...
                # }
                # или "data": {
                #       "items": [...],
                #       "page": 1,
                #       ...
                # }
                # в зависимости от того, как KeyCRM формирует ответ.

                # Смотрим, есть ли "data" (массив)
                if isinstance(data, dict):
                    # Есть вариант:
                    # data["data"] = массив
                    # Или data["data"]["items"] = массив
                    # В документации: "data" — массив объектов
                    # но иногда бывает "data": { "items": [...] }
                    # Нужно проверить структуру. Предположим, что "data" — список продуктов
                    # Но согласно doc: "data": [ {product}, ... ] (paged)
                    # Либо "data": { "items": [...] } (тоже paged)
                    if "data" in data and isinstance(data["data"], list):
                        items = data["data"]
                        all_items.extend(items)
                        # Понять, есть ли еще страницы
                        total = data.get("total", len(all_items))
                        per_page = data.get("per_page", limit)
                        current_page = data.get("current_page", page)
                    elif "data" in data and isinstance(data["data"], dict):
                        # Значит "data": { "items": [...], "total":..., "page":... }
                        sub = data["data"]
                        items = sub.get("items", [])
                        all_items.extend(items)
                        total = sub.get("total", len(all_items))
                        per_page = sub.get("per_page", limit)
                        current_page = sub.get("page", page)
                    else:
                        # fallback: если структура иная, логируем
                        logger.warning("Unexpected JSON structure: %s", data)
                        break

                    # Если мы хотим проверять, есть ли следующая страница
                    # можно посчитать, сколько уже собрали
                    # Если all_items >= total, значит страниц больше нет
                    if len(all_items) >= total:
                        break
                    else:
                        page += 1
                else:
                    logger.warning("Unexpected JSON format: not a dict")
                    break
            else:
                logger.error(f"CRM request failed with status {resp.status_code}")
                break
        except Exception as e:
            logger.error(f"CRM request exception: {e}")
            break

    logger.info(f"Fetched total {len(all_items)} products from CRM (across pages).")
    return all_items

#
# --- FOLLOW-UP LOGIC (NO RESPONSE) ---
#
def no_response_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    message = (
        "Я можу коротко розповісти про наш одноденний тур до зоопарку Ньїредьгаза, Угорщина. "
        "Це шанс подарувати вашій дитині незабутній день серед екзотичних тварин і водночас нарешті відпочити вам. 🦁🐧\n\n"
        "Комфортний автобус, насичена програма і мінімум турбот для вас – все організовано. "
        "Діти отримають море вражень, а ви зможете просто насолоджуватись разом з ними. 🎉\n"
        "Кожен раз наші клієнти повертаються із своїми дітлахами максимально щасливими. "
        "Ви точно полюбите цей тур! 😊"
    )
    context.bot.send_message(chat_id=chat_id, text=message)
    logger.info("No response scenario triggered for chat_id=%s", chat_id)

def schedule_no_response_job(context: CallbackContext, chat_id: int):
    job_queue = context.job_queue
    current_jobs = job_queue.get_jobs_by_name(f"no_response_{chat_id}")
    for job in current_jobs:
        job.schedule_removal()
    job_queue.run_once(
        no_response_callback,
        NO_RESPONSE_DELAY_SECONDS,
        chat_id=chat_id,
        name=f"no_response_{chat_id}",
        data={"message": "Похоже, ви не відповідаєте..."}
    )

def cancel_no_response_job(context: CallbackContext):
    job_queue = context.job_queue
    chat_id = context._chat_id if hasattr(context, '_chat_id') else None
    if chat_id:
        current_jobs = job_queue.get_jobs_by_name(f"no_response_{chat_id}")
        for job in current_jobs:
            job.schedule_removal()

#
# --- HELPER FUNCTIONS ---
#
async def typing_simulation(update: Update, text: str):
    """Отправляет эффект 'набираю сообщение', а затем выводит текст."""
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(2, max(1, len(text)/80)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

def mention_user(update: Update) -> str:
    user = update.effective_user
    return user.first_name if user and user.first_name else "друже"

def is_positive_response(text: str) -> bool:
    positive_keywords = [
        "так", "добре", "да", "ок", "продовжуємо", "розкажіть", "готовий", "готова",
        "привіт", "hello", "расскажи", "зацікав", "зацікавлений"
    ]
    return any(k in text.lower() for k in positive_keywords)

def is_negative_response(text: str) -> bool:
    negative_keywords = ["не хочу", "не можу", "нет", "ні", "не буду", "не зараз"]
    return any(k in text.lower() for k in negative_keywords)

def analyze_intent(text: str) -> str:
    """Пытается определить, положительный ли ответ, отрицательный или непонятный."""
    if nlp_uk:
        doc = nlp_uk(text)
        lemmas = [token.lemma_.lower() for token in doc]
        positive_keywords = {"так", "добре", "да", "ок", "продовжувати", "розповісти", "готовий", "готова", "привіт", "hello", "зацікавити", "зацікавлений"}
        negative_keywords = {"не", "нехочу", "неможу", "нет", "ні", "небуду", "не зараз"}
        if any(kw in lemmas for kw in positive_keywords):
            return "positive"
        if any(kw in lemmas for kw in negative_keywords):
            return "negative"
        return "unclear"
    else:
        if is_positive_response(text):
            return "positive"
        elif is_negative_response(text):
            return "negative"
        else:
            return "unclear"

def get_sentiment(text: str) -> str:
    """Анализ тональности с помощью Transformers."""
    if sentiment_pipeline:
        result = sentiment_pipeline(text)[0]
        try:
            stars = int(result["label"].split()[0])
            if stars <= 2:
                return "negative"
            elif stars == 3:
                return "neutral"
            else:
                return "positive"
        except Exception as e:
            logger.error("Error parsing sentiment result: %s", e)
            return "neutral"
    else:
        return "negative" if is_negative_response(text) else "neutral"

async def get_chatgpt_response(prompt: str) -> str:
    """Вызов ChatGPT для fallback-ответов."""
    if openai is None or not OPENAI_API_KEY:
        return "Вибачте, функція ChatGPT недоступна."
    try:
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Error calling ChatGPT: %s", e)
        return "Вибачте, сталася помилка при генерації відповіді."

#
# --- BOT HANDLERS ---
#

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()
    cancel_no_response_job(context)

    saved_stage, saved_user_data_json = load_user_state(user_id)
    if saved_stage is not None and saved_user_data_json is not None:
        text = (
            "Ви маєте незавершену розмову. "
            "Бажаєте продовжити з того ж місця чи почати заново?\n"
            "Відповідайте: 'Продовжити' або 'Почати заново'."
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET
    else:
        greeting_text = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, greeting_text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

async def greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.strip()
    cancel_no_response_job(context)

    if "продовжити" in user_text.lower():
        saved_stage, saved_data_json = load_user_state(user_id)
        if saved_stage is not None:
            context.user_data.update(json.loads(saved_data_json))
            response_text = "Повертаємось до попередньої розмови."
            await typing_simulation(update, response_text)
            schedule_no_response_job(context, update.effective_chat.id)
            return saved_stage
        else:
            response_text = "Немає попередніх даних, почнемо з нуля."
            await typing_simulation(update, response_text)
            save_user_state(user_id, STAGE_GREET, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_GREET

    if "почати" in user_text.lower() or "заново" in user_text.lower():
        context.user_data.clear()
        greeting_text = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, greeting_text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

    intent = analyze_intent(user_text)
    if intent == "positive":
        response_text = (
            "Дякую за вашу зацікавленість! 😊\n"
            "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DEPARTURE
    elif intent == "negative":
        message = (
            "Я можу коротко розповісти про наш тур, якщо зараз вам незручно відповідати на питання."
        )
        await typing_simulation(update, message)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS

    # Fallback
    fallback_prompt = (
        "В рамках сценарію тура, клієнт написав: " + user_text +
        "\nВідповідай українською мовою, дотримуючись сценарію тура."
    )
    fallback_text = await get_chatgpt_response(fallback_prompt)
    await typing_simulation(update, fallback_text)
    return STAGE_GREET

async def departure_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    departure = update.message.text.strip()
    cancel_no_response_job(context)
    context.user_data["departure"] = departure

    response_text = "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?"
    await typing_simulation(update, response_text)
    save_user_state(user_id, STAGE_TRAVEL_PARTY, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_TRAVEL_PARTY

async def travel_party_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    party_info = update.message.text.lower().strip()
    cancel_no_response_job(context)
    context.user_data["travel_party"] = party_info

    if "дитина" in party_info:
        response_text = "Скільки років вашій дитині?"
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CHILD_AGE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHILD_AGE
    else:
        response_text = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

async def child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    child_age = update.message.text.strip()
    cancel_no_response_job(context)
    context.user_data["child_age"] = child_age

    response_text = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
    await typing_simulation(update, response_text)
    save_user_state(user_id, STAGE_CHOICE, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CHOICE

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    choice_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if "деталь" in choice_text or "деталі" in choice_text:
        context.user_data["choice"] = "details"
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)
    elif "вартість" in choice_text or "ціна" in choice_text:
        context.user_data["choice"] = "cost"
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)
    elif "брон" in choice_text or "бронюй" in choice_text:
        context.user_data["choice"] = "booking"
        response_text = (
            "Я дуже рада, що Ви обрали подорож з нами, це буде дійсно крута поїздка. "
            "Давайте забронюємо місце для вас і вашої дитини. Для цього потрібно внести аванс у розмірі 30% "
            "та надіслати фото паспорта або іншого документу. Після цього я надішлю вам усю необхідну інформацію. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL
    else:
        response_text = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

async def details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cancel_no_response_job(context)
    choice = context.user_data.get("choice", "details")

    # 1) Получаем все продукты (туры) из KeyCRM (все страницы)
    all_products = fetch_all_products()
    if not all_products:
        tours_info = "Наразі немає актуальних турів у CRM або стався збій."
    else:
        tours_info = "Актуальні тури з CRM:\n"
        for p in all_products:
            # Согласно примеру из /products:
            # {
            #   "id": 0,
            #   "name": "Iphone XS max 256gb",
            #   "price": 124.5,
            #   "description": "...",
            #   ...
            # }
            pid = p.get("id", "?")
            pname = p.get("name", "No name")
            pprice = p.get("price", 0)
            pdesc = p.get("description", "")
            tours_info += (
                f"---\n"
                f"ID: {pid}\n"
                f"Назва: {pname}\n"
                f"Ціна: {pprice}\n"
                f"Опис: {pdesc}\n"
            )

    # 2) Базовая информация о туре (статичная)
    if choice == "cost":
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева. 🌟\n"
            "Це цілий день, наповнений пригодами, і вже ввечері ви будете вдома, сповнені приємних спогадів.\n\n"
            "Вартість туру становить 1900 грн з особи. Це ціна, що включає трансфер, квитки до зоопарку, страхування та супровід. "
            "Ви платите один раз і більше не турбуєтеся про жодні організаційні моменти! 🏷️\n\n"
            "Подорож на комфортабельному автобусі із зарядками для гаджетів і клімат-контролем. 🚌\n"
            "Наш супровід вирішує всі організаційні питання в дорозі, а діти отримають море позитивних емоцій! 🎉\n\n"
            f"{tours_info}"
        )
    else:
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева.\n"
            "Тривалість: Цілий день, ввечері Ви вже вдома.\n"
            "Транспорт: Комфортабельний автобус із клімат-контролем та зарядками. 🚌\n"
            "Зоопарк: Більше 500 видів тварин, шоу морських котиків, фото та багато вражень! 🦁\n"
            "Харчування: За власний рахунок, але у нас передбачений час для обіду. 🍽️\n"
            "Додаткові розваги: Після відвідування зоопарку ми заїдемо до великого торгового центру.\n"
            "Вартість туру: 1900 грн з особи. У вартість входить трансфер, квитки до зоопарку, медичне страхування та супровід. 🏷️\n\n"
            f"{tours_info}"
        )

    await typing_simulation(update, text)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    response_followup = "Чи є у вас додаткові запитання щодо програми туру? 😊"
    await update.effective_chat.send_message(text=response_followup)
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)
    
    time_keywords = ["коли виїзд", "коли відправлення", "час виїзду", "коли автобус", "коли вирушаємо"]
    if any(k in user_text for k in time_keywords):
        answer_text = (
            "Ми вирушаємо 26 жовтня о 6:00 з Ужгорода і о 6:30 з Мукачева. "
            "Повертаємось увечері, орієнтовно о 20:00. "
            "Чи є ще запитання? 😊"
        )
        await typing_simulation(update, answer_text)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS
    
    booking_keywords = ["бронювати", "бронюй", "купувати тур", "давай бронювати", "окей давай бронювати", "окей бронюй тур"]
    if any(kw in user_text for kw in booking_keywords):
        response_text = (
            "Добре, переходимо до оформлення бронювання. Я надам вам реквізити для оплати."
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return await close_deal_handler(update, context)
    
    no_more_questions = ["немає", "все зрозуміло", "все ок", "досить", "спасибі", "дякую"]
    if any(k in user_text for k in no_more_questions):
        response_text = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_IMPRESSION, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_IMPRESSION
    
    sentiment = get_sentiment(user_text)
    if sentiment == "negative":
        fallback_prompt = (
            "Клієнт висловив негативне ставлення: " + user_text +
            "\nВідповідай українською мовою, проявляючи емпатію, вибачся та запропонуй допомогу."
        )
        fallback_text = await get_chatgpt_response(fallback_prompt)
        await typing_simulation(update, fallback_text)
        return STAGE_ADDITIONAL_QUESTIONS

    intent = analyze_intent(user_text)
    if intent == "unclear":
        fallback_prompt = (
            "В рамках сценарію тура, клієнт задав нестандартне запитання: " + user_text +
            "\nВідповідай українською мовою, дотримуючись сценарію та проявляючи розуміння."
        )
        fallback_text = await get_chatgpt_response(fallback_prompt)
        await typing_simulation(update, fallback_text)
        return STAGE_ADDITIONAL_QUESTIONS

    answer_text = "Гарне запитання! Якщо є ще щось, що вас цікавить, будь ласка, питайте."
    await typing_simulation(update, answer_text + "\n\nЧи є ще запитання?")
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ADDITIONAL_QUESTIONS

async def impression_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)
    
    positive_keywords = ["добре", "клас", "цікаво", "відмінно", "супер", "підходить", "так"]
    negative_keywords = ["ні", "не цікаво", "дорого", "завелика", "надто"]
    if any(k in user_text for k in positive_keywords):
        response_text = (
            "Чудово! 🎉 Давайте забронюємо місце для вас і вашої дитини, щоб забезпечити комфортний відпочинок. "
            "Для цього потрібно внести аванс у розмірі 30% та надіслати фото паспорта або іншого документу. "
            "Після цього я надішлю вам усю необхідну інформацію.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL
    elif any(k in user_text for k in negative_keywords):
        response_text = (
            "Шкода це чути. Якщо у вас залишилися питання або ви захочете розглянути інші варіанти, звертайтеся."
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        response_text = "Дякую за думку! Чи готові ви переходити до бронювання?"
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)
    
    positive_keywords = ["приват", "моно", "оплачу", "готов", "готова", "давайте"]
    if any(k in user_text for k in positive_keywords):
        response_text = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Після оплати надішліть, будь ласка, скріншот для підтвердження бронювання."
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT
    negative_keywords = ["ні", "нет", "не буду", "не хочу"]
    if any(k in user_text for k in negative_keywords):
        response_text = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END

    response_text = (
        "Дякую! Ви готові завершити оформлення?\n"
        "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
    )
    await typing_simulation(update, response_text)
    save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CLOSE_DEAL

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)
    
    if any(keyword in user_text for keyword in ["оплатив", "відправив", "скинув", "готово"]):
        response_text = (
            "Дякую! Тепер перевірю надходження. Як тільки все буде ок, я надішлю деталі поїздки і підтвердження бронювання!"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_PAYMENT_CONFIRM, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT_CONFIRM
    else:
        response_text = (
            "Якщо виникли додаткові питання — я на зв'язку. Потрібна допомога з оплатою?"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

async def payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cancel_no_response_job(context)
    response_text = (
        "Дякую за бронювання! 🎉 Ми успішно зберегли за вами місце в турі до зоопарку Ньїредьгаза. "
        "Найближчим часом я надішлю всі деталі (список речей, час виїзду тощо). "
        "Якщо є питання, звертайтеся. Ми завжди на зв'язку!"
    )
    await typing_simulation(update, response_text)
    save_user_state(user_id, STAGE_END, context.user_data)
    return STAGE_END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name if user else "Unknown")
    text = "Гаразд, завершуємо розмову. Якщо виникнуть питання, завжди можете звернутися знову!"
    await typing_simulation(update, text)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_END, context.user_data)
    return ConversationHandler.END

@app.route('/')
def index():
    return "Сервер працює! Бот активний."

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        global application
        if not application:
            logger.error("Application is not initialized yet.")
            return "No application available"
        update = Update.de_json(data, application.bot)
        loop = application.bot_data.get("loop")
        if loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
            logger.info("Webhook received and processed.")
        else:
            logger.error("No event loop available to process update.")
    return "OK"

async def setup_webhook(url, app_ref):
    webhook_url = f"{url}/webhook"
    await app_ref.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to: {webhook_url}")

async def run_bot():
    if is_bot_already_running():
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)
    logger.info("Starting bot...")
    req = HTTPXRequest(connect_timeout=20, read_timeout=40)
    application_builder = Application.builder().token(BOT_TOKEN).request(req)
    global application
    application = application_builder.build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_GREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, greet_handler)],
            STAGE_DEPARTURE: [MessageHandler(filters.TEXT & ~filters.COMMAND, departure_handler)],
            STAGE_TRAVEL_PARTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, travel_party_handler)],
            STAGE_CHILD_AGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, child_age_handler)],
            STAGE_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, choice_handler)],
            STAGE_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, details_handler)],
            STAGE_ADDITIONAL_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, additional_questions_handler)],
            STAGE_IMPRESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, impression_handler)],
            STAGE_CLOSE_DEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, close_deal_handler)],
            STAGE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handler)],
            STAGE_PAYMENT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_confirm_handler)],
            STAGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND,
                                       lambda update, context: context.bot.send_message(
                                           chat_id=update.effective_chat.id,
                                           text="Дякую! Якщо виникнуть питання — /start."
                                       ))],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)

    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()
    loop = asyncio.get_running_loop()
    application.bot_data["loop"] = loop
    logger.info("Bot is online and ready.")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Bot thread started. Now starting Flask...")
    start_flask()
