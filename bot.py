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
import requests

try:
    from pydantic import BaseModel, Field, ValidationError
except ImportError:
    BaseModel = None

try:
    import spacy
    nlp_uk = spacy.load("uk_core_news_sm")
except:
    nlp_uk = None

try:
    import openai
except:
    openai = None

try:
    from transformers import pipeline
    sentiment_pipeline = pipeline("sentiment-analysis", model="nlptown/bert-base-multilingual-uncased-sentiment")
except:
    sentiment_pipeline = None

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CRM_API_KEY = os.getenv("CRM_API_KEY")
CRM_API_URL = os.getenv("CRM_API_URL", "https://familyplace.keycrm.app/api/v1/products")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", 'https://your-app.onrender.com')

if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# Глобальный кэш с турами, чтобы не ходить в CRM всякий раз
CRM_CACHE = []

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

if BaseModel is not None:
    class ProductModel(BaseModel):
        id: int
        name: str
        price: float = Field(0.0)
        description: str = Field("", alias="description")

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

NO_RESPONSE_DELAY_SECONDS = 6 * 3600

app = Flask(__name__)
application = None

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

def fetch_all_products():
    """
    Базовый метод: достаём туры из CRM.
    Теперь будет вызываться раз в N минут через планировщик,
    а также при старте для первичного наполнения кэша CRM_CACHE.
    """
    if not CRM_API_KEY or not CRM_API_URL:
        logger.warning("CRM_API_KEY or CRM_API_URL not found. Returning empty tours list.")
        return []
    headers = {"Authorization": f"Bearer {CRM_API_KEY}", "Accept": "application/json"}
    all_items = []
    page = 1
    limit = 50

    while True:
        logger.info("Attempting to fetch from CRM... page=%s", page)
        params = {"page": page, "limit": limit}
        try:
            resp = requests.get(CRM_API_URL, headers=headers, params=params, timeout=10)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    logger.error(f"Failed to parse JSON. Response text: {resp.text}")
                    break

                if isinstance(data, dict):
                    if "data" in data and isinstance(data["data"], list):
                        items = data["data"]
                        for it in items:
                            if BaseModel is not None:
                                try:
                                    validated = ProductModel(**it)
                                    all_items.append(validated.dict())
                                except ValidationError:
                                    logger.warning("Invalid product data %s", it)
                            else:
                                all_items.append(it)
                        total = data.get("total", len(all_items))
                    else:
                        logger.warning("Unexpected JSON structure: %s", data)
                        break

                    if len(all_items) >= total:
                        break
                    else:
                        page += 1
                else:
                    logger.warning("Unexpected JSON format: not a dict, got %r", data)
                    break
            else:
                logger.error(f"CRM request failed with status {resp.status_code}")
                break
        except Exception as e:
            logger.error(f"CRM request exception: {e}")
            break

    logger.info(f"Fetched total {len(all_items)} products from CRM (across pages).")
    return all_items

def no_response_callback(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    text = (
        "Я можу коротко розповісти про наш одноденний тур до зоопарку Ньїредьгаза, Угорщина. "
        "Це шанс подарувати вашій дитині незабутній день серед екзотичних тварин і водночас нарешті відпочити вам. "
        "Комфортний автобус, насичена програма і мінімум турбот – все організовано. "
        "Діти отримають море вражень, а ви зможете просто насолоджуватись разом з ними. "
        "Кожен раз наші клієнти повертаються із своїми дітлахами максимально щасливими. "
        "Ви точно полюбите цей тур! 😊"
    )
    context.bot.send_message(chat_id=chat_id, text=text)
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

async def typing_simulation(update: Update, text: str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(4, max(2, len(text)/70)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

# ======================
# Дополнительная логика
# ======================

def detect_toxic(text: str) -> bool:
    rude = ["дурак","тупой","ебнулся","идиот","stfu","fuck","shit","блять","сука","хуй","придурок"]
    return any(r in text.lower() for r in rude)

def is_positive_response(text: str) -> bool:
    arr = [
        "так","добре","да","ок","продовжуємо","продовжити","продовжить","розкажіть",
        "готовий","готова","привіт","hello","зацікав","зацікавлений",
        "ага","хочу","буду","yes","интересно","интересует","заинтересован","зацікавлен","готов"
    ]
    return any(k in text.lower() for k in arr)

def is_negative_response(text: str) -> bool:
    arr = [
        "не хочу","не можу","нет","ні","не буду","не зараз","отмена","cancel",
        "передумав","передумала","неинтересно","надоело","хватит"
    ]
    return any(k in text.lower() for k in arr)

# Пример «умного» анализа намерения через GPT
async def chatgpt_intent_analysis(text: str) -> str:
    """
    С помощью GPT пытаемся отнести ответ к одной из категорий:
    'details', 'cost', 'booking', 'negative', 'positive', 'unclear'.
    """
    if openai is None or not OPENAI_API_KEY:
        return "unclear"
    system_prompt = (
        "Ты — бот по продаже однодневных туров в зоопарк Ньїредьгаза, Угорщина. "
        "Тебе дано сообщение пользователя: оцени, к какому это относится:\n"
        "1) details — пользователь хочет узнать детали тура (что включено, программа);\n"
        "2) cost — спрашивает цену/стоимость;\n"
        "3) booking — хочет забронировать/купить/заплатить;\n"
        "4) negative — отказывает/не хочет;\n"
        "5) positive — соглашается/да (продолжить разговор);\n"
        "6) unclear — не понятно.\n\n"
        "Отвечай одним словом на английском: details / cost / booking / negative / positive / unclear."
    )
    user_prompt = f"User message: '{text}'"
    try:
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=10,
            temperature=0.0
        )
        cat = response.choices[0].message.content.strip().lower()
        # Подстрахуемся
        if cat not in ["details","cost","booking","negative","positive","unclear"]:
            return "unclear"
        return cat
    except Exception as e:
        logger.error("chatgpt_intent_analysis error: %s", e)
        return "unclear"

def get_sentiment(text: str) -> str:
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
        except:
            return "neutral"
    else:
        return "negative" if is_negative_response(text) else "neutral"

async def get_chatgpt_response(prompt: str) -> str:
    if openai is None or not OPENAI_API_KEY:
        return "Вибачте, функція ChatGPT недоступна."
    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты — бот, специализирующийся на однодневном туре в зоопарк Ньїредьгаза. "
                    "Отвечай коротко, позитивно, используя дружелюбный стиль, чётко соблюдай сценарий тура. "
                    "Если вопрос выходит за рамки, всё равно постарайся вплести упоминание зоопарка "
                    "или кратко извинись и предложи вернуться к туризму."
                )
            },
            {"role": "user", "content": prompt}
        ]
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-4",  # Или "gpt-3.5-turbo"
            messages=messages,
            max_tokens=300,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Error calling ChatGPT: %s", e)
        return "Вибачте, сталася помилка при генерації відповіді."

# ======================
# Обработчики состояний
# ======================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()
    cancel_no_response_job(context)
    stg, dat = load_user_state(user_id)
    if stg is not None and dat is not None:
        short_reminder = "У вас є незавершена розмова."
        if stg == STAGE_CLOSE_DEAL:
            short_reminder = "Ви зупинилися на етапі бронювання та оплати."
        elif stg == STAGE_DETAILS:
            short_reminder = "Ви зупинилися на етапі уточнення деталей туру."
        elif stg == STAGE_ADDITIONAL_QUESTIONS:
            short_reminder = "Ви зупинилися на етапі додаткових питань про тур."

        text = (
            f"{short_reminder}\n"
            "Бажаєте продовжити з того ж місця чи почати заново?\n"
            "Відповідайте: 'Продовжити' або 'Почати заново'."
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET
    else:
        txt = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, txt)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

async def greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip().lower()
    cancel_no_response_job(context)

    # 1) Токсичность?
    if detect_toxic(txt):
        await typing_simulation(update, "Давайте збережемо приємну атмосферу розмови 😊. Чи є у вас запитання щодо туру?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    # 2) Пользователь хочет продолжить?
    if "продовжити" in txt:
        stg, dat = load_user_state(user_id)
        if stg is not None:
            context.user_data.update(json.loads(dat))
            resp = "Повертаємось до попередньої розмови."
            await typing_simulation(update, resp)
            schedule_no_response_job(context, update.effective_chat.id)
            return stg
        else:
            r = "Немає попередніх даних, почнемо з нуля."
            await typing_simulation(update, r)
            save_user_state(user_id, STAGE_GREET, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_GREET

    # 3) Пользователь хочет начать заново?
    if "почати" in txt or "заново" in txt:
        context.user_data.clear()
        g = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, g)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

    # 4) Проверяем intent "вручную"
    if is_positive_response(txt):
        t = (
            "Дякую за вашу зацікавленість! 😊\n"
            "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        )
        await typing_simulation(update, t)
        save_user_state(user_id, STAGE_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DEPARTURE
    elif is_negative_response(txt):
        m = (
            "Я можу коротко розповісти про наш тур, якщо зараз вам незручно відповідати на питання."
        )
        await typing_simulation(update, m)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS

    # 5) Если наши простые проверки не помогли — вызываем GPT
    intent_gpt = await chatgpt_intent_analysis(txt)
    if intent_gpt in ["positive"]:
        await typing_simulation(update, "Чудово! Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌")
        save_user_state(user_id, STAGE_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DEPARTURE
    elif intent_gpt in ["negative"]:
        await typing_simulation(update, "Добре, тоді можу коротко розповісти або закінчити? Що ви обираєте?")
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS
    elif intent_gpt in ["details"]:
        await typing_simulation(update, "Зрозуміло, давайте розкажу детальніше про програму туру...")
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS
    elif intent_gpt in ["cost"]:
        await typing_simulation(update, "Звісно, розкажу про вартість і що входить в тур.")
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS
    elif intent_gpt in ["booking"]:
        await typing_simulation(update, "Ви бажаєте забронювати місце? Тоді перейдімо до оформлення!")
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

    # Если и GPT не дало результата -> fallback
    fallback_text = await get_chatgpt_response(
        "Ось питання від клієнта: " + txt +
        "\nВідповідай українською мовою, дотримуючись сценарію одноденного туру до зоопарку Ньїредьгаза."
    )
    await typing_simulation(update, fallback_text)
    # Остаёмся в том же состоянии
    return STAGE_GREET

async def departure_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    d = update.message.text.strip()
    cancel_no_response_job(context)

    if detect_toxic(d):
        await typing_simulation(update, "Будь ласка, збережемо повагу в розмові. Чим я можу допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    context.user_data["departure"] = d
    r = "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?"
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_TRAVEL_PARTY, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_TRAVEL_PARTY

async def travel_party_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if detect_toxic(txt):
        await typing_simulation(update, "Прошу зберігати ввічливість. Ви хотіли щось спитати про тур?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    if "дит" in txt or "child" in txt or "ребен" in txt:
        context.user_data["travel_party"] = "child"
        await typing_simulation(update, "Скільки років вашій дитині?")
        save_user_state(user_id, STAGE_CHILD_AGE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHILD_AGE

    context.user_data["travel_party"] = "no_child"
    r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_CHOICE, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CHOICE

async def child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    t = update.message.text.strip()
    cancel_no_response_job(context)

    if detect_toxic(t):
        await typing_simulation(update, "Я вас почула, але прошу без образ. Чим я можу допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    if t.isdigit():
        context.user_data["child_age"] = t
        r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

    if any(x in t.lower() for x in ["детал","вартість","ціна","брон","cost","details"]):
        context.user_data["child_age"] = "unspecified"
        rr = "Добре, перейдемо далі."
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

    await typing_simulation(update, "Будь ласка, вкажіть вік дитини або задайте інше питання.")
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CHILD_AGE

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if detect_toxic(txt):
        await typing_simulation(update, "Будь ласка, залишаймося у привітній розмові. Чим можу допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    if "детал" in txt or "details" in txt:
        context.user_data["choice"] = "details"
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)
    elif "вартість" in txt or "ціна" in txt or "cost" in txt:
        context.user_data["choice"] = "cost"
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)
    elif "брон" in txt or "bронируй" in txt or "booking" in txt or "забронировать" in txt or "поїхати" in txt:
        context.user_data["choice"] = "booking"
        r = (
            "Я дуже рада, що Ви обрали подорож з нами, це буде дійсно крута поїздка. "
            "Давайте забронюємо місце для вас і вашої дитини. Для цього потрібно внести аванс у розмірі 30% "
            "та надіслати фото паспорта або іншого документу. Після цього я надішлю вам усю необхідну інформацію. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

    resp = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
    await typing_simulation(update, resp)
    save_user_state(user_id, STAGE_CHOICE, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CHOICE

async def details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cancel_no_response_job(context)
    choice = context.user_data.get("choice","details")

    # Берём из кэша, чтобы не бегать в CRM каждый раз
    relevant = []
    for p in CRM_CACHE:
        nm = (p.get("name","") or "").lower()
        pr = p.get("price", 0)
        # ищем по слову "ніред", "нир", "зоопарк", + pr>0
        if ("ніред" in nm or "нир" in nm or "зоопарк" in nm) and pr > 0:
            relevant.append(p)

    if not relevant:
        tours_info = "Наразі немає актуальних (або ціна=0) турів у CRM, пов'язаних із Ньїредьгазою."
    else:
        if len(relevant) == 1:
            p = relevant[0]
            pname = p.get("name","No name")
            pprice = p.get("price",0)
            pdesc = p.get("description","") or "Без опису"
            tours_info = f"Тур: {pname}\nЦіна: {pprice}\nОпис: {pdesc}"
        else:
            tours_info = "Ось актуальні варіанти зоопарку Ньїредьгаза:\n"
            for p in relevant:
                pid = p.get("id","?")
                pname = p.get("name","No name")
                pprice = p.get("price",0)
                tours_info += f"- {pname} (ID {pid}), ціна: {pprice}\n"

    if choice == "cost":
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода (6:00) та Мукачева (6:30).\n"
            "Це цілий день, і ввечері ви будете вдома.\n"
            "Вартість туру: 1900 грн з особи (трансфер, квитки, страхування).\n\n"
            + tours_info
        )
    else:
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода (6:00) чи Мукачева (6:30).\n"
            "Тривалість: Цілий день, повертаємось орієнтовно о 20:00.\n"
            "Транспорт: Комфортабельний автобус.\n"
            "Зоопарк: Більше 500 видів тварин.\n"
            "Вартість: 1900 грн (трансфер, квитки, страхування).\n\n"
            + tours_info
        )

    await typing_simulation(update, text)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    await update.effective_chat.send_message(text="Чи є у вас додаткові запитання щодо програми туру? 😊")
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if detect_toxic(txt):
        await typing_simulation(update, "Прошу говорити ввічливо. Чим можу допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    time_keys = [
        "коли виїзд","коли відправлення","час виїзду","коли автобус","коли вирушаємо",
        "во сколько выезд","сколько выезд","о котрій виїзд","когда выезд"
    ]
    if any(k in txt for k in time_keys):
        ans = (
            "Виїзд о 6:00 з Ужгорода, о 6:30 з Мукачева, повертаємось орієнтовно о 20:00.\n"
            "Чи є ще запитання?"
        )
        await typing_simulation(update, ans)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    book_keys = [
        "бронювати","бронюй","купувати тур","давай бронювати","окей давай бронювати",
        "окей бронюй тур","бронируй","хочу поїхати","хочу забронировать","давай поедем","поедем","поехать"
    ]
    if any(k in txt for k in book_keys):
        r = "Добре, переходимо до оформлення бронювання. Я надам вам реквізити для оплати."
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return await close_deal_handler(update, context)

    no_more = ["немає","все зрозуміло","все ок","досить","спасибі","дякую","спасибо","нет вопросов","ok"]
    if any(k in txt for k in no_more):
        rr = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_IMPRESSION, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_IMPRESSION

    sentiment = get_sentiment(txt)
    if sentiment == "negative":
        fp = (
            "Клієнт висловив негативне ставлення: " + txt +
            "\nВідповідай коротко та позитивно, проявляючи емпатію, вибачся та запропонуй допомогу."
        )
        fallback_text = await get_chatgpt_response(fp)
        await typing_simulation(update, fallback_text)
        return STAGE_ADDITIONAL_QUESTIONS

    # GPT fallback при непонятном вопросе
    prompt = (
        "Клієнт задав нестандартне запитання: " + txt +
        "\nВідповідай коротко, згадуй зоопарк Ньїредьгаза і відповідай позитивно."
    )
    fb = await get_chatgpt_response(prompt)
    await typing_simulation(update, fb)
    return STAGE_ADDITIONAL_QUESTIONS

async def impression_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if detect_toxic(txt):
        await typing_simulation(update, "Залишаймося доброзичливими, будь ласка. Чим можу допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    pos = ["добре","клас","цікаво","відмінно","супер","підходить","так","отлично","круто","подходит"]
    neg = ["ні","не цікаво","дорого","завелика","надто","не подходит","нет","не хочу"]
    if any(k in txt for k in pos):
        r = (
            "Чудово! 🎉 Давайте забронюємо місце для вас і вашої дитини, щоб забезпечити комфортний відпочинок. "
            "Для цього потрібно внести аванс у розмірі 30% та надіслати фото паспорта або іншого документу. "
            "Після цього я надішлю вам усю необхідну інформацію.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL
    elif any(k in txt for k in neg):
        rr = "Шкода це чути. Якщо у вас залишилися питання або ви захочете розглянути інші варіанти, звертайтеся."
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        resp = "Дякую за думку! Чи готові ви переходити до бронювання?"
        await typing_simulation(update, resp)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if detect_toxic(txt):
        await typing_simulation(update, "Будь ласка, з повагою. Чим я можу допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    pos = [
        "приват","моно","оплачу","готов","готова","давайте","monobank","privatbank",
        "оплатил","оплатила","я перевел","я перевела","ok"
    ]
    if any(k in txt for k in pos):
        r = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Після оплати надішліть, будь ласка, скріншот для підтвердження бронювання."
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

    neg = ["ні","нет","не буду","не хочу","отмена","cancel","передумав","не сейчас"]
    if any(k in txt for k in neg):
        r2 = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, r2)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END

    r3 = "Дякую! Ви готові завершити оформлення? Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
    await typing_simulation(update, r3)
    save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CLOSE_DEAL

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if detect_toxic(txt):
        await typing_simulation(update, "Прошу, без образ. Чи можу я ще чимось допомогти?")
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    if any(k in txt for k in ["оплатив","відправив","скинув","готово","сплатив","перевел","перевела"]):
        r = (
            "Дякую! Тепер перевірю надходження. Як тільки все буде ок, я надішлю деталі поїздки і підтвердження бронювання!"
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_PAYMENT_CONFIRM, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT_CONFIRM
    else:
        rr = "Якщо виникли додаткові питання — я на зв'язку. Потрібна допомога з оплатою?"
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

async def payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    cancel_no_response_job(context)
    r = (
        "Дякую за бронювання! Ми успішно зберегли за вами місце. Найближчим часом я надішлю всі деталі. "
        "Якщо є питання — пишіть!"
    )
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_END, context.user_data)
    return STAGE_END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name if user else "Unknown")
    t = "Гаразд, завершуємо розмову. Якщо виникнуть питання, завжди можете звернутися знову!"
    await typing_simulation(update, t)
    uid = str(update.effective_user.id)
    save_user_state(uid, STAGE_END, context.user_data)
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

async def scheduled_crm_update(context: CallbackContext):
    """
    Периодически обновляем CRM_CACHE.
    """
    global CRM_CACHE
    logger.info("Running scheduled CRM update...")
    new_data = fetch_all_products()
    CRM_CACHE = new_data  # Перезаписываем
    logger.info("CRM_CACHE updated with %d products.", len(CRM_CACHE))

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

    # Изначально заполняем CRM_CACHE
    global CRM_CACHE
    CRM_CACHE = fetch_all_products()

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
                lambda u,c: c.bot.send_message(chat_id=u.effective_chat.id,
                                               text="Дякую! Якщо виникнуть питання — /start."))]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)

    await setup_webhook(WEBHOOK_URL, application)

    # Запланировать периодическое обновление CRM, например, раз в час
    job_queue = application.job_queue
    job_queue.run_repeating(scheduled_crm_update, interval=3600, first=10)

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
