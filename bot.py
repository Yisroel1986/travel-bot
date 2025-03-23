import os
import logging
import sys
import psutil
import sqlite3
import json
from datetime import datetime
import asyncio
import threading
import requests

from dotenv import load_dotenv
from flask import Flask, request

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
# Попытка подключить spaCy, openai, huggingface
# -----------------------------
try:
    import spacy
    nlp_uk = spacy.load("uk_core_news_sm")  # украинская модель spaCy
except:
    nlp_uk = None

try:
    import openai
except:
    openai = None

try:
    from transformers import pipeline
    sentiment_pipeline = pipeline("sentiment-analysis", model="cardiffnlp/twitter-roberta-base-sentiment-latest")
except:
    sentiment_pipeline = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CRM_API_KEY = os.getenv("CRM_API_KEY")
CRM_API_URL = os.getenv("CRM_API_URL", "")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "")


# Если есть ключ openai, используем
if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

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
# СЦЕНАРНЫЕ ТЕКСТЫ (вместо scenario.py)
# -----------------------------

# ---- Детский лагерь "Лапландия в Карпатах"
LAPLANDIA_INTRO = (
    "Вітаю! 😊 Дякую за інтерес до нашого зимового табору 'Лапландія в Карпатах'. "
    "Щоб надати вам детальну інформацію та відповісти на всі питання, "
    "будь ласка, надішліть ваш номер телефону. Наш менеджер зв'яжеться з вами найближчим часом. "
    "📞 Куди вам буде зручніше отримати інформацію: у Viber чи Telegram?"
)

LAPLANDIA_IF_PHONE = (
    "Дякую! 📲 Передаю ваш номер нашому менеджеру, вона зв'яжеться з вами найближчим часом.\n"
    "Якщо хочете, можу коротко розповісти деталі табору 'Лапландія в Карпатах' прямо тут, у чаті?"
)

LAPLANDIA_BRIEF = (
    "У таборі 'Лапландія в Карпатах' кожен день — це казка! Ваша дитина порине у зимову магію, "
    "де кожен день наповнений пригодами.\n\n"
    "• Ранкові снігові активності: катаня на лижах, санках, снігові бої.\n"
    "• Майстер-класи та творчі майданчики: малювання, створення новорічних прикрас та кулінарні уроки.\n"
    "• Вечірні активності: дискотеки, квести, вечірні посиденьки біля каміну.\n"
    "• Екскурсії до унікальних місць (оленяча ферма, зимові ліси Карпат).\n\n"
    "Вартість: 17,200 грн. Але зараз діє акція раннього бронювання — 16,200 грн! "
    "Ця сума включає все необхідне: проживання, харчування, страховку, супровід вихователів та всі активності."
)

LAPLANDIA_NO_PHONE = (
    "Зрозуміло, що ви поки не готові залишити номер. Тоді давайте я відповім на ваші питання тут. "
    "Дозвольте задати кілька уточнюючих питань, щоб підібрати найкращий варіант для вашої дитини. Добре?"
)

# ---- Автобусный тур "Венгерский зоопарк"
ZOO_INTRO = (
    "Вітаю! 😊 Дякую за інтерес до одноденного туру в зоопарк Ньїредьгаза, Угорщина. "
    "Це чудова можливість подарувати вашій дитині та вам незабутній день серед екзотичних тварин! "
    "Дозвольте задати кілька уточнюючих питань. Добре?"
)

ZOO_DETAILS = (
    "Дата виїзду: 26 жовтня (з Ужгорода чи Мукачева).\n"
    "Тривалість: Цілий день, ввечері ви вже вдома.\n"
    "Транспорт: Комфортабельний автобус з клімат-контролем та зарядками. 🚌\n"
    "Зоопарк: Понад 500 видів тварин, шоу морських котиків, фото, багато вражень! 🦁\n"
    "Вартість туру: 1900 грн з людини (включає трансфер, квитки, страховку, супровід).\n"
    "Після зоопарку: Заїдемо в великий торговий центр, де можна відпочити, зробити покупки чи випити каву."
)

# ---- Общий fallback
FALLBACK_TEXT = (
    "Вибачте, я поки не зрозуміла вашого питання. Я можу розповісти про зимовий табір 'Лапландія в Карпатах' "
    "чи одноденний тур в зоопарк Ньїредьгаза. Будь ласка, уточніть, що саме вас цікавить. 😊"
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
# DB init / load / save
# ============================
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

def load_user_state(user_id:str):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT current_stage,user_data FROM conversation_state WHERE user_id=?",(user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None,None

def save_user_state(user_id:str, stage:int, user_data:dict):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    ud_json = json.dumps(user_data, ensure_ascii=False)
    now = datetime.now().isoformat()
    c.execute("""
        INSERT OR REPLACE INTO conversation_state 
        (user_id, current_stage, user_data, last_interaction)
        VALUES (?,?,?,?)
    """,(user_id, stage, ud_json, now))
    conn.commit()
    conn.close()

# ============================
# No response job
# ============================
def no_response_callback(context:CallbackContext):
    chat_id = context.job.chat_id
    text = (
        "Схоже, що ви зайняті. Якщо бажаєте дізнатися більше про наші пропозиції (зимовий табір чи зоопарк), "
        "пишіть мені, я завжди на зв'язку! 😊"
    )
    context.bot.send_message(chat_id=chat_id, text=text)

def schedule_no_response_job(context:CallbackContext, chat_id:int):
    jq = context.job_queue
    jobs = jq.get_jobs_by_name(f"noresp_{chat_id}")
    for j in jobs:
        j.schedule_removal()
    jq.run_once(no_response_callback, NO_RESPONSE_DELAY_SECONDS, chat_id=chat_id, name=f"noresp_{chat_id}")

def cancel_no_response_job(context:CallbackContext):
    jq = context.job_queue
    chat_id = context._chat_id if hasattr(context,'_chat_id') else None
    if chat_id:
        jobs = jq.get_jobs_by_name(f"noresp_{chat_id}")
        for j in jobs:
            j.schedule_removal()

# ============================
# Typing simulation
# ============================
async def typing_simulation(update:Update, text:str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(4, max(2, len(text)/70)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

# ============================
# Intent detection
# ============================
def is_positive_response(txt:str)->bool:
    arr = ["так","добре","да","ок","продовжуємо","розкажіть","готовий","готова","привіт","hello","yes","зацікав","sure"]
    return any(k in txt.lower() for k in arr)

def is_negative_response(txt:str)->bool:
    arr = ["не хочу","не можу","нет","ні","не буду","не зараз","no"]
    return any(k in txt.lower() for k in arr)

def analyze_intent(txt:str)->str:
    if nlp_uk:
        doc = nlp_uk(txt)
        lemmas = [t.lemma_.lower() for t in doc]
        if any(k in lemmas for k in ["так","ок","добре","готовий"]):
            return "positive"
        if any(k in lemmas for k in ["не","ні","нет","небуду"]):
            return "negative"
        return "unclear"
    else:
        if is_positive_response(txt):
            return "positive"
        elif is_negative_response(txt):
            return "negative"
        else:
            return "unclear"

# ============================
# GPT fallback
# ============================
async def gpt_fallback_response(user_text:str, context:dict = None)->str:
    if not openai or not OPENAI_API_KEY:
        return "Вибачте, функція GPT недоступна."
    try:
        system_prompt = (
            "Ты — чат-бот-женщина по имени Олена, дружелюбная, эмпатичная, "
            "работаешь на украинском языке для продажи туров и детских лагерей. "
            "Твоя задача — помочь клиентам выбрать подходящий тур или лагерь, "
            "отвечать на их вопросы вежливо и тепло, подчеркивать преимущества "
            "каждого предложения. Всегда отвечай на украинском языке, даже если "
            "вопрос задан на другом языке. Отвечай одним четким сообщением, "
            "структурированным по пунктам. Если это ответ на нестандартный вопрос, "
            "используй формат чата, но сохраняй структуру ответа.\n\n"
            "Важные правила:\n"
            "1. Всегда отвечай на украинском языке\n"
            "2. Сохраняй контекст разговора\n"
            "3. Следуй сценарию продаж\n"
            "4. Не обрезай сообщения\n"
            "5. Отвечай одним сообщением"
        )
        
        # Добавляем контекст разговора, если он есть
        context_str = ""
        if context:
            context_str = f"\nКонтекст разговора:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
            
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Вопрос клиента: {user_text}{context_str}"}
        ]
        
        resp = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-4",
            messages=messages,
            max_tokens=1000,  # Увеличиваем лимит токенов
            temperature=0.7
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"GPT error: {e}")
        return FALLBACK_TEXT

# ============================
# START Handler
# ============================
async def start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()
    cancel_no_response_job(context)

    stage, data = load_user_state(user_id)
    if stage is not None:
        text = (
            "У вас є незавершена розмова. Хочете продовжити з того ж місця чи почати заново?\n"
            "Відповідайте: 'Продовжити' чи 'Почати заново'."
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE
    else:
        txt = (
            "Вітаю! Дякую за інтерес до наших пропозицій. "
            "Скажіть, будь ласка, що вас цікавить: зимовий табір 'Лапландія в Карпатах' "
            "чи одноденний тур в зоопарк Ньїредьгаза? 😊"
        )
        await typing_simulation(update, txt)
        save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE

# ============================
# SCENARIO CHOICE
# ============================
async def scenario_choice_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    # Лагерь
    if any(k in txt for k in ["лапланд","карпат","лагерь","camp"]):
        context.user_data["scenario"] = "camp"
        text = LAPLANDIA_INTRO
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_CAMP_PHONE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_PHONE

    # Зоопарк
    elif any(k in txt for k in ["зоопарк","ньиредьхаза","nyire","лев","одноден","мукач","ужгород"]):
        context.user_data["scenario"] = "zoo"
        text = ZOO_INTRO
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_ZOO_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_GREET

    else:
        # GPT fallback с контекстом
        prompt = (
            f"Пользователь написал: {txt}\n"
            "Нужно определить, интересует ли лагерь 'Лапландия' или 'Зоопарк Ньиредьхаза'. "
            "Если непонятно, попроси уточнить."
        )
        gpt_text = await gpt_fallback_response(prompt, context.user_data)
        await typing_simulation(update, gpt_text)
        save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE

# ============================
# CAMP: PHONE
# ============================
async def camp_phone_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    phone_candidate = txt.replace(" ","").replace("-","")
    if phone_candidate.startswith("+") or phone_candidate.isdigit():
        # пользователь дал телефон
        context.user_data["phone"] = phone_candidate
        r = LAPLANDIA_IF_PHONE
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CAMP_DETAILED, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_DETAILED
    else:
        # не дал телефон
        r = LAPLANDIA_NO_PHONE
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CAMP_NO_PHONE_QA, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA

# ============================
# CAMP: NO PHONE Q/A
# ============================
async def camp_no_phone_qa_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    intent = analyze_intent(txt)
    if intent == "positive":
        # задаём уточняющие вопросы
        r = "З якого ви міста? 🏙️"
        await typing_simulation(update, r)
        context.user_data["camp_questions"] = 1
        save_user_state(user_id, STAGE_CAMP_NO_PHONE_QA, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA
    else:
        fallback = "Будь ласка, уточніть, чи можемо ми поговорити детальніше про табір?"
        await typing_simulation(update, fallback)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA

# ============================
# CAMP: DETAILED
# ============================
async def camp_detailed_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    # Проверяем, не является ли сообщение ответом на вопрос о деталях
    if "так" in txt.lower() or "добре" in txt.lower() or "розкажіть" in txt.lower():
        r = LAPLANDIA_BRIEF
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CAMP_END, context.user_data)
        return STAGE_CAMP_END
    else:
        # Если это не ответ на вопрос о деталях, используем GPT
        prompt = (
            f"Клієнт написав: {txt}\n"
            "Контекст: Клієнт зацікавлений зимовим табором 'Лапландія в Карпатах'. "
            "Вже надав номер телефону. Потрібно відповісти на українській мові, "
            "зберігаючи дружній тон та структуру відповіді."
        )
        gpt_text = await gpt_fallback_response(prompt, context.user_data)
        await typing_simulation(update, gpt_text)
        save_user_state(user_id, STAGE_CAMP_DETAILED, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_DETAILED

# ============================
# CAMP: END
# ============================
async def camp_end_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Дякую! Якщо виникнуть питання — /start. Гарного дня!")
    return ConversationHandler.END

# ============================
# ZOO: Greet
# ============================
async def zoo_greet_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    intent = analyze_intent(txt)
    if intent == "positive":
        r = "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_ZOO_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_DEPARTURE
    elif intent == "negative":
        msg = (
            "Я можу коротко розповісти про наш одноденний тур, якщо вам незручно відповідати на питання. "
            "Це займе буквально хвилину!"
        )
        await typing_simulation(update, msg)
        save_user_state(user_id, STAGE_ZOO_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_DETAILS
    else:
        prompt = (
            f"Клієнт написав: {txt}\n"
            "Якщо незрозуміло, попросіть уточнити (сценарій зоопарк)."
        )
        fallback = await gpt_fallback_response(prompt, context.user_data)
        await typing_simulation(update, fallback)
        return STAGE_ZOO_GREET

async def zoo_departure_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    context.user_data["departure"] = txt
    r = "Для кого ви розглядаєте цю поїздку? Плануєте їхати разом з дитиною?"
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_ZOO_TRAVEL_PARTY, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_TRAVEL_PARTY

async def zoo_travel_party_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower().strip()

    if "дит" in txt:
        await typing_simulation(update, "Скільки років вашій дитині?")
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CHILD_AGE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CHILD_AGE
    else:
        r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CHOICE

async def zoo_child_age_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
    await typing_simulation(update, r)
    save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_CHOICE

async def zoo_choice_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower().strip()

    if "детал" in txt:
        context.user_data["choice"] = "details"
        save_user_state(str(update.effective_user.id), STAGE_ZOO_DETAILS, context.user_data)
        return await zoo_details_handler(update, context)
    elif "варт" in txt or "цін" in txt:
        context.user_data["choice"] = "cost"
        save_user_state(str(update.effective_user.id), STAGE_ZOO_DETAILS, context.user_data)
        return await zoo_details_handler(update, context)
    elif "брон" in txt:
        context.user_data["choice"] = "booking"
        r = (
            "Я дуже рада, що ви обрали подорож з нами. "
            "Давайте забронюємо місце для вас та вашої дитини. "
            "Для цього потрібно внести аванс 30% та надіслати фото паспорта. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        )
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL
    else:
        resp = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
        await typing_simulation(update, resp)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CHOICE

async def zoo_details_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower()
    choice = context.user_data.get("choice","details")

    if choice == "cost":
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева.\n"
            "Це цілий день, і ввечері ви вже вдома.\n"
            "Вартість туру: 1900 грн (включає трансфер, квитки, страховку).\n\n"
            "Уявіть, як ваша дитина буде в захваті від зустрічі з левами, слонами та жирафами, а ви "
            "зможете насолодитися прогулянкою без зайвих турбот. "
            "Чи є у вас додаткові питання?"
        )
    else:
        text = ZOO_DETAILS

    await typing_simulation(update, text)
    save_user_state(str(update.effective_user.id), STAGE_ZOO_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_QUESTIONS

async def zoo_questions_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower()

    if "брон" in txt:
        r = "Чудово, тоді переходимо до оформлення бронювання. Я надішлю реквізити для оплати!"
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        return STAGE_ZOO_CLOSE_DEAL
    else:
        # Используем GPT для нестандартных вопросов
        prompt = (
            f"Клієнт написав: {txt}\n"
            "Контекст: Клієнт зацікавлений туром в зоопарк Ньїредьхаза. "
            "Вже отримав інформацію про тур. Потрібно відповісти на українській мові, "
            "зберігаючи дружній тон та структуру відповіді."
        )
        gpt_text = await gpt_fallback_response(prompt, context.user_data)
        await typing_simulation(update, gpt_text)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_QUESTIONS

async def zoo_impression_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower()

    if is_positive_response(txt):
        r = (
            "Чудово! 🎉 Давайте забронюємо місце. "
            "Потрібно внести аванс 30% та надіслати фото паспорта. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        )
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL
    elif is_negative_response(txt):
        rr = "Шкода це чути. Якщо будуть питання — я завжди тут!"
        await typing_simulation(update, rr)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_END, context.user_data)
        return STAGE_ZOO_END
    else:
        fallback = "Дякую за думку! Чи готові ви до бронювання?"
        await typing_simulation(update, fallback)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL

async def zoo_close_deal_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower()

    if any(k in txt for k in ["приват","моно","оплат","готов","давайте","скинь","реквізит"]):
        r = (
            "Чудово! Ось реквізити:\n"
            "Картка: 0000 0000 0000 0000\n\n"
            "Як оплатите — надішліть, будь ласка, скрін. Після цього я надішлю програму та підтвердження бронювання!"
        )
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_PAYMENT
    elif is_negative_response(txt):
        r2 = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, r2)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_END, context.user_data)
        return STAGE_ZOO_END
    else:
        r3 = "Ви готові завершити оформлення? Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        await typing_simulation(update, r3)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_CLOSE_DEAL

async def zoo_payment_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    txt = update.message.text.lower()

    if any(k in txt for k in ["оплат","відправ","готово","скинув","чек"]):
        r = "Дякую! Перевірю надходження та надішлю деталі!"
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_PAYMENT_CONFIRM, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_PAYMENT_CONFIRM
    else:
        rr = "Якщо виникнуть питання з оплатою — пишіть, я допоможу."
        await typing_simulation(update, rr)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_PAYMENT

async def zoo_payment_confirm_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    r = (
        "Дякую за бронювання! Ваше місце офіційно заброньовано. "
        "Незабаром надішлю повну інформацію. Якщо будуть питання — звертайтесь!"
    )
    await typing_simulation(update, r)
    return ConversationHandler.END

# ============================
# /cancel
# ============================
async def cancel_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    logger.info("User canceled conversation")
    t = "Добре, завершуємо розмову. Якщо виникнуть питання, звертайтесь знову!"
    await typing_simulation(update, t)
    return ConversationHandler.END

# ============================
# Глобальный fallback
# ============================
async def global_fallback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    """
    Сюда попадаем, если ConversationHandler не забрал сообщение
    (т.е. никакой стейт не подошёл).
    """
    user_text = update.message.text.strip()
    gpt_text = await gpt_fallback_response(user_text, context.user_data)
    await typing_simulation(update, gpt_text)

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
            logger.error("No application.")
            return "No application"
        update = Update.de_json(data, application.bot)
        loop = application.bot_data.get("loop")
        if loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), loop)
        else:
            logger.error("No event loop to process update.")
    return "OK"

async def setup_webhook(url:str, app_ref):
    wh_url = f"{url}/webhook"
    await app_ref.bot.set_webhook(wh_url)
    logger.info("Webhook set to %s", wh_url)

async def run_bot():
    if is_bot_already_running():
        logger.error("Another instance is running. Exiting.")
        sys.exit(1)
    logger.info("Starting bot...")

    req = HTTPXRequest(connect_timeout=20, read_timeout=40)
    global application
    builder = ApplicationBuilder().token(BOT_TOKEN).request(req)
    application = builder.build()

    # ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_SCENARIO_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, scenario_choice_handler)
            ],
            # Camp
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
                               lambda u,c: c.bot.send_message(u.effective_chat.id,
                               "Дякую! Якщо виникнуть питання — /start."))
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler, group=0)

    # Глобальный fallback (если ConversationHandler не перехватил)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, global_fallback_handler),
        group=1
    )

    # Настройка webhook
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

if __name__=="__main__":
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Bot thread started. Now starting Flask...")
    start_flask()
