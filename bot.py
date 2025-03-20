import os
import sys
import json
import psutil
import asyncio
import threading
from datetime import datetime

import aiosqlite  # Асинхронная работа с SQLite
import requests

from dotenv import load_dotenv
from flask import Flask, request

# -----------------------------
# Замена logging -> loguru, но уровень WARNING
# -----------------------------
from loguru import logger
logger.remove()  # Удаляем дефолтный handler
logger.add(
    "bot.log",
    rotation="1 MB",
    retention="3 days",
    level="WARNING"  # Только WARNING и выше
)

from telegram import (
    Update,
    ReplyKeyboardRemove
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    CallbackContext,
    filters
)
from telegram.request import HTTPXRequest

# -----------------------------
# spaCy: стараемся не загружать большую модель
# -----------------------------
try:
    import spacy
    # Предполагаем, что если модель не найдена, делаем заглушку
    try:
        nlp_uk = spacy.load("uk_core_news_sm")
        logger.warning("spaCy (uk_core_news_sm) loaded successfully.")
    except Exception as e:
        logger.warning(f"spaCy not loaded: {e}")
        nlp_uk = spacy.blank("uk")  # Заглушка
except ImportError:
    logger.warning("spaCy is not installed. Using blank pipeline.")
    nlp_uk = None

# -----------------------------
# LangChain + OpenAI
# Отключим трейсинг, чтобы не хранить историю в памяти
# -----------------------------
try:
    import openai
    from langchain.llms import OpenAI
    # При желании отключаем трейсинг:
    # from langchain_core.tracing import disable_tracing
    # disable_tracing()

    openai_api_key = os.getenv("OPENAI_API_KEY")
    if openai_api_key:
        openai.api_key = openai_api_key
        # Указываем минимальный модельный подход, без torch
        # Если GPT-4 недоступен, замените на 'gpt-3.5-turbo'
        llm = OpenAI(
            model_name="gpt-3.5-turbo",
            openai_api_key=openai_api_key,
            temperature=0.7
        )
    else:
        llm = None
        logger.warning("No OPENAI_API_KEY found.")
except Exception as e:
    logger.warning(f"LangChain/OpenAI not available: {e}")
    llm = None

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "")

# -----------------------------
# Проверка, не запущен ли бот вторым процессом
# -----------------------------
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

# -----------------------------
# Сценарные тексты (встроенные)
# -----------------------------
LAPLANDIA_INTRO = (
    "Вітаю Вас! 😊 Дякую за Ваш інтерес до нашого зимового табору 'Лапландія в Карпатах'. "
    "Щоб надати Вам детальну інформацію та відповісти на всі Ваші запитання, "
    "надішліть, будь ласка, Ваш номер телефону. Наш менеджер зателефонує Вам у найближчий час. "
    "📞 Вам куди буде зручніше отримати інформацію: на Viber чи Telegram?"
)
LAPLANDIA_IF_PHONE = (
    "Дякую! 📲 Передаю Ваш номер нашому менеджеру, вона зв'яжеться з Вами найближчим часом.\n"
    "Якщо бажаєте, можу коротко розповісти деталі табору 'Лапландія в Карпатах' прямо тут, у чаті?"
)
LAPLANDIA_BRIEF = (
    "У таборі 'Лапландія в Карпатах' кожен день — це казка! Ваша дитина порине у зимову магію, "
    "де кожен день сповнений пригод.\n\n"
    "• Ранкові снігові активності...\n"
    "• Майстер-класи та творчі майданчики...\n"
    "• Вечірні активності...\n"
    "• Екскурсії...\n\n"
    "Вартість: 17,200 грн. Але зараз діє акція раннього бронювання — 16,200 грн!"
)
LAPLANDIA_NO_PHONE = (
    "Розумію, що ви поки не готові залишити номер. Тоді давайте я відповім на ваші запитання тут. "
    "Дозвольте задати кілька уточнюючих питань, щоб підібрати кращий варіант для вашої дитини. Добре?"
)

ZOO_INTRO = (
    "Вітаю вас! 😊 Дякую за Ваш інтерес до одноденного туру в зоопарк Ньїредьгаза. "
    "Це чудова можливість подарувати вашій дитині та вам незабутній день серед екзотичних тварин! "
    "Дозвольте задати кілька уточнюючих питань. Добре?"
)
ZOO_DETAILS = (
    "Дата виїзду: 26 жовтня.\n"
    "Тривалість: Цілий день.\n"
    "Транспорт: Комфортабельний автобус.\n"
    "Вартість: 1900 грн (трансфер, квитки, страхування).\n\n"
    "Після зоопарку заїдемо в торговий центр."
)

FALLBACK_TEXT = (
    "Вибачте, я поки що не зрозуміла вашого запитання. "
    "Я можу розповісти про табір 'Лапландія' або тур до зоопарку Ньїредьгаза. "
    "Будь ласка, уточніть, що саме вас цікавить. 😊"
)

# -----------------------------
# Conversation states
# -----------------------------
(
    STAGE_SCENARIO_CHOICE,  
    STAGE_CAMP_PHONE,       
    STAGE_CAMP_NO_PHONE_QA, 
    STAGE_CAMP_DETAILED,    
    STAGE_CAMP_END,         

    STAGE_ZOO_GREET,        
    STAGE_ZOO_DEPARTURE,    
    STAGE_ZOO_TRAVEL_PARTY, 
    STAGE_ZOO_CHILD_AGE,    
    STAGE_ZOO_CHOICE,       
    STAGE_ZOO_DETAILS,      
    STAGE_ZOO_QUESTIONS,    
    STAGE_ZOO_IMPRESSION,   
    STAGE_ZOO_CLOSE_DEAL,   
    STAGE_ZOO_PAYMENT,      
    STAGE_ZOO_PAYMENT_CONFIRM,
    STAGE_ZOO_END           
) = range(17)

NO_RESPONSE_DELAY_SECONDS = 6*3600

app = Flask(__name__)
application = None

# ============================
# DB init
# ============================
async def init_db():
    async with aiosqlite.connect("bot_database.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversation_state (
                user_id TEXT PRIMARY KEY,
                current_stage INTEGER,
                user_data TEXT,
                last_interaction TIMESTAMP
            )
        """)
        await db.commit()

async def load_user_state(user_id: str):
    try:
        async with aiosqlite.connect("bot_database.db") as db:
            cursor = await db.execute(
                "SELECT current_stage, user_data FROM conversation_state WHERE user_id=?",
                (user_id,)
            )
            row = await cursor.fetchone()
            if row:
                return row[0], row[1]
            return None, None
    except Exception as e:
        logger.warning(f"load_user_state error: {e}")
        return None, None

async def save_user_state(user_id: str, stage: int, user_data: dict):
    try:
        ud_json = json.dumps(user_data, ensure_ascii=False)
        now = datetime.now().isoformat()
        async with aiosqlite.connect("bot_database.db") as db:
            await db.execute(
                """INSERT OR REPLACE INTO conversation_state 
                   (user_id, current_stage, user_data, last_interaction)
                   VALUES (?,?,?,?)""",
                (user_id, stage, ud_json, now)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"save_user_state error: {e}")

# ============================
# No response job
# ============================
def no_response_callback(context: CallbackContext):
    chat_id = context.job.chat_id
    text = (
        "Схоже, що ви зайняті. Якщо бажаєте дізнатися більше про наші пропозиції, "
        "пишіть мені, я завжди на зв'язку! 😊"
    )
    context.bot.send_message(chat_id=chat_id, text=text)

def schedule_no_response_job(context: CallbackContext, chat_id: int):
    jq = context.job_queue
    jobs = jq.get_jobs_by_name(f"noresp_{chat_id}")
    for j in jobs:
        j.schedule_removal()
    jq.run_once(no_response_callback, NO_RESPONSE_DELAY_SECONDS, chat_id=chat_id, name=f"noresp_{chat_id}")

def cancel_no_response_job(context: CallbackContext):
    jq = context.job_queue
    chat_id = context._chat_id if hasattr(context,'_chat_id') else None
    if chat_id:
        jobs = jq.get_jobs_by_name(f"noresp_{chat_id}")
        for j in jobs:
            j.schedule_removal()

# ============================
# Typing simulation
# ============================
async def typing_simulation(update: Update, text: str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(4, max(2, len(text) / 70)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

# ============================
# Minimal intent detection (spaCy)
# ============================
def analyze_intent(txt: str) -> str:
    if nlp_uk:
        doc = nlp_uk(txt.lower())
        lemmas = [t.lemma_.lower() for t in doc]
        if any(k in lemmas for k in ["так","ок","добре","готовий","хочу"]):
            return "positive"
        if any(k in lemmas for k in ["не","ні","нет","не хочу","не буду"]):
            return "negative"
        return "unclear"
    else:
        # Простейшая логика, если spaCy нет
        txt_low = txt.lower()
        if any(k in txt_low for k in ["так","ок","добре","готов","хочу"]):
            return "positive"
        if any(k in txt_low for k in ["не","ні","нет","не хочу"]):
            return "negative"
        return "unclear"

# ============================
# GPT fallback (через OpenAI API или LangChain)
# ============================
async def gpt_fallback_response(user_text: str) -> str:
    if not llm:
        return FALLBACK_TEXT  # fallback

    prompt = (
        "Ты — бот по продаже туров (Лапландия, зоопарк). "
        "Если вопрос не по теме, отвечай: 'Извините, я могу помочь только по теме туров.'\n\n"
        f"Пользователь: {user_text}\nОтвет:"
    )
    try:
        response = await asyncio.to_thread(llm, prompt)
        return response.strip()
    except Exception as e:
        logger.warning(f"GPT fallback error: {e}")
        return FALLBACK_TEXT

# ============================
# Conversation Handlers
# ============================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    await init_db()
    cancel_no_response_job(context)

    stg, _ = await load_user_state(user_id)
    if stg is not None:
        text = (
            "Ви маєте незавершену розмову. Бажаєте продовжити з того ж місця чи почати заново?\n"
            "Відповідайте: 'Продовжити' або 'Почати заново'."
        )
        await typing_simulation(update, text)
        await save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE
    else:
        txt = (
            "Вітаю! Дякую за інтерес до наших пропозицій. "
            "Скажіть, будь ласка, що вас цікавить: табір 'Лапландія' чи тур у зоопарк Ньїредьгаза?"
        )
        await typing_simulation(update, txt)
        await save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE

async def scenario_choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    # camp
    if any(k in txt for k in ["лапланд","карпат","табір","лагерь","camp"]):
        context.user_data["scenario"] = "camp"
        await typing_simulation(update, LAPLANDIA_INTRO)
        await save_user_state(user_id, STAGE_CAMP_PHONE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_PHONE
    # zoo
    elif any(k in txt for k in ["зоопарк","ніредьгаза","nyire","лев","одноден","мукач","ужгород"]):
        context.user_data["scenario"] = "zoo"
        await typing_simulation(update, ZOO_INTRO)
        await save_user_state(user_id, STAGE_ZOO_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_GREET
    else:
        # GPT fallback
        answer = await gpt_fallback_response(txt)
        await typing_simulation(update, answer)
        await save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE

async def camp_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    phone_candidate = txt.replace(" ","").replace("-","")
    if phone_candidate.startswith("+") or phone_candidate.isdigit():
        await typing_simulation(update, LAPLANDIA_IF_PHONE)
        await save_user_state(user_id, STAGE_CAMP_DETAILED, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_DETAILED
    else:
        await typing_simulation(update, LAPLANDIA_NO_PHONE)
        await save_user_state(user_id, STAGE_CAMP_NO_PHONE_QA, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA

async def camp_no_phone_qa_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    intent = analyze_intent(txt)
    if intent == "positive":
        msg = "З якого Ви міста? 🏙️"
        await typing_simulation(update, msg)
        context.user_data["camp_questions"] = 1
        await save_user_state(user_id, STAGE_CAMP_NO_PHONE_QA, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA
    else:
        fallback = "Будь ласка, уточніть, чи можемо ми поговорити детальніше про табір?"
        await typing_simulation(update, fallback)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA

async def camp_detailed_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    await typing_simulation(update, LAPLANDIA_BRIEF)
    await save_user_state(user_id, STAGE_CAMP_END, context.user_data)
    return STAGE_CAMP_END

async def camp_end_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Дякую! Якщо виникнуть питання — /start. Гарного дня!")
    return ConversationHandler.END

# ---- Zoo
async def zoo_greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    intent = analyze_intent(txt)
    if intent == "positive":
        msg = "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        await typing_simulation(update, msg)
        await save_user_state(user_id, STAGE_ZOO_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_DEPARTURE
    elif intent == "negative":
        msg = (
            "Я можу коротко розповісти про наш одноденний тур, якщо вам незручно відповідати на питання. "
            "Це займе буквально хвилину!"
        )
        await typing_simulation(update, msg)
        await save_user_state(user_id, STAGE_ZOO_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_DETAILS
    else:
        fallback = await gpt_fallback_response(txt)
        await typing_simulation(update, fallback)
        return STAGE_ZOO_GREET

async def zoo_departure_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    context.user_data["departure"] = txt
    r = "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?"
    await typing_simulation(update, r)
    await save_user_state(user_id, STAGE_ZOO_TRAVEL_PARTY, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_TRAVEL_PARTY

async def zoo_travel_party_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower().strip()

    if "дит" in txt:
        context.user_data["travel_party"] = "child"
        await typing_simulation(update, "Скільки років вашій дитині?")
        await save_user_state(str(update.effective_user.id), STAGE_ZOO_CHILD_AGE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CHILD_AGE
    else:
        context.user_data["travel_party"] = "no_child"
        r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, r)
        await save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CHOICE

async def zoo_child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
    await typing_simulation(update, r)
    await save_user_state(user_id, STAGE_ZOO_CHOICE, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_CHOICE

async def zoo_choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    if "детал" in txt:
        context.user_data["choice"] = "details"
        await save_user_state(user_id, STAGE_ZOO_DETAILS, context.user_data)
        return await zoo_details_handler(update, context)
    elif "вартість" in txt or "ціна" in txt:
        context.user_data["choice"] = "cost"
        await save_user_state(user_id, STAGE_ZOO_DETAILS, context.user_data)
        return await zoo_details_handler(update, context)
    elif "брон" in txt:
        context.user_data["choice"] = "booking"
        r = (
            "Я дуже рада, що Ви обрали подорож з нами. "
            "Давайте забронюємо місце для вас і вашої дитини. "
            "Для цього потрібно внести аванс 30% та надіслати фото паспорта. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        )
        await typing_simulation(update, r)
        await save_user_state(user_id, STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL
    else:
        resp = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
        await typing_simulation(update, resp)
        await save_user_state(user_id, STAGE_ZOO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CHOICE

async def zoo_details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    choice = context.user_data.get("choice","details")

    if choice == "cost":
        text = (
            "Дата виїзду: 26 жовтня. Вартість туру: 1900 грн (включає трансфер, квитки, страхування).\n\n"
            "Уявіть, як ваша дитина в захваті від зустрічі з левами, жирафами, а ви "
            "можете насолодитися прогулянкою без турбот. "
            "Чи є у вас додаткові запитання?"
        )
    else:
        text = ZOO_DETAILS

    await typing_simulation(update, text)
    await save_user_state(user_id, STAGE_ZOO_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_QUESTIONS

async def zoo_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower()

    if "брон" in txt:
        r = "Чудово, тоді переходимо до оформлення бронювання. Я надішлю реквізити для оплати!"
        await typing_simulation(update, r)
        await save_user_state(user_id, STAGE_ZOO_CLOSE_DEAL, context.user_data)
        return STAGE_ZOO_CLOSE_DEAL
    else:
        msg = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, msg)
        await save_user_state(user_id, STAGE_ZOO_IMPRESSION, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_IMPRESSION

async def zoo_impression_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower()

    if any(k in txt for k in ["так","ок","добре","готов","зацікав"]):
        r = (
            "Чудово! 🎉 Давайте забронюємо місце. "
            "Потрібно внести аванс 30% і надіслати фото паспорта. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        )
        await typing_simulation(update, r)
        await save_user_state(user_id, STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL
    elif any(k in txt for k in ["ні","нет","не"]):
        rr = "Шкода це чути. Якщо будуть питання — я завжди тут!"
        await typing_simulation(update, rr)
        await save_user_state(user_id, STAGE_ZOO_END, context.user_data)
        return STAGE_ZOO_END
    else:
        fallback = "Дякую за думку! Чи готові ви до бронювання?"
        await typing_simulation(update, fallback)
        await save_user_state(user_id, STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL

async def zoo_close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower()

    if any(k in txt for k in ["приват","моно","оплачу","готов","давайте","скинь","реквизит"]):
        r = (
            "Чудово! Ось реквізити:\n"
            "Картка: 0000 0000 0000 0000\n\n"
            "Як оплатите — надішліть скрін. Після цього я відправлю програму і підтвердження!"
        )
        await typing_simulation(update, r)
        await save_user_state(user_id, STAGE_ZOO_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_PAYMENT
    elif any(k in txt for k in ["ні","нет","не"]):
        r2 = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, r2)
        await save_user_state(user_id, STAGE_ZOO_END, context.user_data)
        return STAGE_ZOO_END
    else:
        r3 = "Ви готові завершити оформлення? Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        await typing_simulation(update, r3)
        await save_user_state(user_id, STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL

async def zoo_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower()

    if any(k in txt for k in ["оплатив","відправив","готово","скинув","чек"]):
        r = "Дякую! Перевірю надходження і відправлю деталі!"
        await typing_simulation(update, r)
        await save_user_state(user_id, STAGE_ZOO_PAYMENT_CONFIRM, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_PAYMENT_CONFIRM
    else:
        rr = "Якщо виникли питання з оплатою — пишіть, я допоможу."
        await typing_simulation(update, rr)
        await save_user_state(user_id, STAGE_ZOO_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_PAYMENT

async def zoo_payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    r = (
        "Дякую за бронювання! Ваше місце офіційно заброньоване. "
        "Скоро надішлю повну інформацію. Якщо будуть питання — звертайтесь!"
    )
    await typing_simulation(update, r)
    return ConversationHandler.END

# ---- /cancel
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    logger.warning("User canceled conversation")
    t = "Добре, завершуємо розмову. Якщо виникнуть питання, звертайтесь знову!"
    await typing_simulation(update, t)
    return ConversationHandler.END

# ---- Глобальный fallback
async def global_fallback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.strip()
    fallback_answer = await gpt_fallback_response(user_text)
    await typing_simulation(update, fallback_answer)

# ============================
# Flask endpoints
# ============================
@app.route('/')
def index():
    return "Сервер працює! Бот активний."

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        global application
        if not application:
            logger.warning("No application.")
            return "No application"
        update = Update.de_json(data, application.bot)
        loop = application.bot_data.get("loop")
        if loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
        else:
            logger.warning("No event loop to process update.")
    return "OK"

async def setup_webhook(url: str, app_ref):
    if not url:
        logger.warning("No RENDER_EXTERNAL_URL provided, skipping set_webhook.")
        return
    wh_url = f"{url}/webhook"
    await app_ref.bot.set_webhook(wh_url)
    logger.warning(f"Webhook set to {wh_url}")

async def run_bot():
    if is_bot_already_running():
        logger.warning("Another instance is running. Exiting.")
        sys.exit(1)
    logger.warning("Starting bot...")

    req = HTTPXRequest(connect_timeout=20, read_timeout=40)
    global application
    builder = ApplicationBuilder().token(BOT_TOKEN).request(req)
    # drop_pending_updates=True — чтобы не было дублирующихся сообщений
    application = builder.build()

    # Инициализация БД (async)
    await init_db()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_SCENARIO_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, scenario_choice_handler)
            ],
            STAGE_CAMP_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, camp_phone_handler)
            ],
            STAGE_CAMP_NO_PHONE_QA: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, camp_no_phone_qa_handler)
            ],
            STAGE_CAMP_DETAILED: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, camp_detailed_handler)
            ],
            STAGE_CAMP_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, camp_end_handler)
            ],
            # Zoo
            STAGE_ZOO_GREET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_greet_handler)
            ],
            STAGE_ZOO_DEPARTURE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_departure_handler)
            ],
            STAGE_ZOO_TRAVEL_PARTY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_travel_party_handler)
            ],
            STAGE_ZOO_CHILD_AGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_child_age_handler)
            ],
            STAGE_ZOO_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_choice_handler)
            ],
            STAGE_ZOO_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_details_handler)
            ],
            STAGE_ZOO_QUESTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_questions_handler)
            ],
            STAGE_ZOO_IMPRESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_impression_handler)
            ],
            STAGE_ZOO_CLOSE_DEAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_close_deal_handler)
            ],
            STAGE_ZOO_PAYMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_payment_handler)
            ],
            STAGE_ZOO_PAYMENT_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, zoo_payment_confirm_handler)
            ],
            STAGE_ZOO_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                    lambda u, c: c.bot.send_message(u.effective_chat.id, "Дякую! Якщо виникнуть питання — /start."))
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler, group=0)

    # Глобальный fallback
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, global_fallback_handler),
        group=1
    )

    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()

    loop = asyncio.get_running_loop()
    application.bot_data["loop"] = loop

    logger.warning("Bot is online and ready.")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.warning(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.warning("Bot thread started. Now starting Flask...")
    start_flask()
