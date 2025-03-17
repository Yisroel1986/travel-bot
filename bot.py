# bot.py

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
from telegram import Update, ReplyKeyboardRemove
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackContext
)
from telegram.request import HTTPXRequest

# spaCy, openai, huggingface
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
    sentiment_pipeline = pipeline("sentiment-analysis", model="cardiffnlp/twitter-roberta-base-sentiment-latest")
except:
    sentiment_pipeline = None

from scenario import ScenarioData
from gpt_logic import GPTLogic

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "")

if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

app = Flask(__name__)
application = None

gpt_logic = GPTLogic()

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

# -----------------------------
# DB
# -----------------------------
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

# -----------------------------
# No response callback
# -----------------------------
def no_response_callback(context:CallbackContext):
    chat_id = context.job.chat_id
    text = ScenarioData.get_no_response_fallback()
    context.bot.send_message(chat_id=chat_id, text=text)

def schedule_no_response_job(context:CallbackContext, chat_id:int):
    jq = context.job_queue
    jobs = jq.get_jobs_by_name(f"noresp_{chat_id}")
    for j in jobs:
        j.schedule_removal()
    jq.run_once(no_response_callback, NO_RESPONSE_DELAY_SECONDS, 
                chat_id=chat_id, name=f"noresp_{chat_id}")

def cancel_no_response_job(context:CallbackContext):
    jq = context.job_queue
    chat_id = getattr(context, '_chat_id', None)
    if chat_id:
        jobs = jq.get_jobs_by_name(f"noresp_{chat_id}")
        for j in jobs:
            j.schedule_removal()

# -----------------------------
# Typing simulation
# -----------------------------
async def typing_simulation(update:Update, text:str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(4, max(2, len(text)/70)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

# -----------------------------
# Intent detection
# -----------------------------
def is_positive_response(txt:str)->bool:
    arr = ["так","добре","да","ок","продовжуємо","розкажіть","готовий","готова","привіт","hello","зацікав","yes","sure"]
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

# -----------------------------
# GPT fallback
# -----------------------------
async def gpt_fallback(user_text:str) -> str:
    return gpt_logic.get_fallback_response(user_text)

# -----------------------------
# START
# -----------------------------
async def start_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()
    cancel_no_response_job(context)

    stg, _ = load_user_state(user_id)
    if stg is not None:
        txt = (
            "Ви маєте незавершену розмову. Бажаєте продовжити з того ж місця чи почати заново?\n"
            "Відповідайте: 'Продовжити' або 'Почати заново'."
        )
        await typing_simulation(update, txt)
        save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE
    else:
        greet = (
            "Вітаю! Дякую за інтерес до наших пропозицій. "
            "Скажіть, будь ласка, що вас цікавить: зимовий табір 'Лапландія в Карпатах' "
            "чи одноденний тур у зоопарк Ньїредьгаза? 😊"
        )
        await typing_simulation(update, greet)
        save_user_state(user_id, STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE

# -----------------------------
# SCENARIO CHOICE
# -----------------------------
async def scenario_choice_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    # Лагерь
    if any(k in txt for k in ["лапланд","карпат","табір","лагерь","camp"]):
        context.user_data["scenario"] = "camp"
        await typing_simulation(update, ScenarioData.LAPLANDIA_INTRO)
        save_user_state(str(update.effective_user.id), STAGE_CAMP_PHONE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_PHONE

    # Зоопарк
    elif any(k in txt for k in ["зоопарк","ніредьгаза","nyire","лев","одноден","мукач","ужгород"]):
        context.user_data["scenario"] = "zoo"
        await typing_simulation(update, ScenarioData.ZOO_INTRO)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_GREET

    else:
        prompt = (
            f"Клієнт написав: {txt}\n"
            "Невідомо, чи він хоче 'Лапландія' або 'Зоопарк'. Попроси уточнити."
        )
        fallback_answer = await gpt_fallback(prompt)
        await typing_simulation(update, fallback_answer)
        save_user_state(str(update.effective_user.id), STAGE_SCENARIO_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_SCENARIO_CHOICE

# --------------- CAMP ---------------
async def camp_phone_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    phone_candidate = txt.replace(" ","").replace("-","")
    if phone_candidate.startswith("+") or phone_candidate.isdigit():
        await typing_simulation(update, ScenarioData.LAPLANDIA_IF_PHONE_PROVIDED)
        save_user_state(str(update.effective_user.id), STAGE_CAMP_DETAILED, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_DETAILED
    else:
        await typing_simulation(update, ScenarioData.LAPLANDIA_NO_PHONE)
        save_user_state(str(update.effective_user.id), STAGE_CAMP_NO_PHONE_QA, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA

async def camp_no_phone_qa_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    intent = analyze_intent(txt)
    if intent == "positive":
        # задаём уточняющие вопросы
        q = "З якого Ви міста? 🏙️"
        await typing_simulation(update, q)
        context.user_data["camp_questions"] = 1
        save_user_state(str(update.effective_user.id), STAGE_CAMP_NO_PHONE_QA, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA
    else:
        fallback = "Будь ласка, уточніть, чи можемо ми поговорити детальніше про табір?"
        await typing_simulation(update, fallback)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CAMP_NO_PHONE_QA

async def camp_detailed_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    text = ScenarioData.LAPLANDIA_BRIEF_DETAILS + "\nНаш менеджер зв'яжеться з вами для деталей!"
    await typing_simulation(update, text)
    save_user_state(str(update.effective_user.id), STAGE_CAMP_END, context.user_data)
    return STAGE_CAMP_END

async def camp_end_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Дякую! Якщо виникнуть питання — /start.")
    return ConversationHandler.END

# --------------- ZOO ---------------
async def zoo_greet_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    intent = analyze_intent(txt)
    if intent == "positive":
        msg = "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        await typing_simulation(update, msg)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_DEPARTURE
    elif intent == "negative":
        fallback_msg = (
            "Я можу коротко розповісти про наш одноденний тур, якщо вам незручно відповідати на питання. "
            "Це займе буквально хвилину!"
        )
        await typing_simulation(update, fallback_msg)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ZOO_DETAILS
    else:
        prompt = (
            f"Клієнт написав: {txt}\n"
            "Сценарій: Зоопарк. Якщо незрозуміло, попроси уточнити."
        )
        fb = await gpt_fallback(prompt)
        await typing_simulation(update, fb)
        return STAGE_ZOO_GREET

async def zoo_departure_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    context.user_data["departure"] = txt
    r = "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?"
    await typing_simulation(update, r)
    save_user_state(str(update.effective_user.id), STAGE_ZOO_TRAVEL_PARTY, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ZOO_TRAVEL_PARTY

async def zoo_travel_party_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    if "дит" in txt:
        context.user_data["travel_party"] = "child"
        await typing_simulation(update, "Скільки років вашій дитині?")
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CHILD_AGE, context.user_data)
        return STAGE_ZOO_CHILD_AGE
    else:
        context.user_data["travel_party"] = "no_child"
        msg = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, msg)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
        return STAGE_ZOO_CHOICE

async def zoo_child_age_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    msg = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
    await typing_simulation(update, msg)
    save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
    return STAGE_ZOO_CHOICE

async def zoo_choice_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    if "детал" in txt:
        context.user_data["choice"] = "details"
        save_user_state(str(update.effective_user.id), STAGE_ZOO_DETAILS, context.user_data)
        return await zoo_details_handler(update, context)
    elif "вартість" in txt or "ціна" in txt:
        context.user_data["choice"] = "cost"
        save_user_state(str(update.effective_user.id), STAGE_ZOO_DETAILS, context.user_data)
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
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        return STAGE_ZOO_CLOSE_DEAL
    else:
        resp = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
        await typing_simulation(update, resp)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CHOICE, context.user_data)
        return STAGE_ZOO_CHOICE

async def zoo_details_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    choice = context.user_data.get("choice","details")
    if choice == "cost":
        text = ScenarioData.ZOO_PRICE_SCENARIO + "\nЧи є у вас додаткові запитання?"
    else:
        text = ScenarioData.ZOO_DETAILS + "\nЯкщо хочете дізнатися більше — я на зв'язку!"
    await typing_simulation(update, text)
    save_user_state(str(update.effective_user.id), STAGE_ZOO_QUESTIONS, context.user_data)
    return STAGE_ZOO_QUESTIONS

async def zoo_questions_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    if "брон" in txt:
        r = "Чудово, тоді переходимо до оформлення бронювання. Я надішлю реквізити для оплати!"
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
        return STAGE_ZOO_CLOSE_DEAL
    else:
        msg = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, msg)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_IMPRESSION, context.user_data)
        return STAGE_ZOO_IMPRESSION

async def zoo_impression_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    if is_positive_response(txt):
        r = (
            "Чудово! 🎉 Давайте забронюємо місце. "
            "Потрібно внести аванс 30% і надіслати фото паспорта. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        )
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_CLOSE_DEAL, context.user_data)
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
        return STAGE_ZOO_CLOSE_DEAL

async def zoo_close_deal_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    if any(k in txt for k in ["приват","моно","оплачу","готов","давайте","скинь","реквизит"]):
        r = (
            "Чудово! Ось реквізити:\n"
            "Картка: 0000 0000 0000 0000\n\n"
            "Як оплатите — надішліть, будь ласка, скрін. Після цього я відправлю програму і підтвердження бронювання!"
        )
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_PAYMENT, context.user_data)
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
        return STAGE_ZOO_CLOSE_DEAL

async def zoo_payment_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    if any(k in txt for k in ["оплатив","відправив","готово","скинув","чек"]):
        r = "Дякую! Перевірю надходження і відправлю деталі!"
        await typing_simulation(update, r)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_PAYMENT_CONFIRM, context.user_data)
        return STAGE_ZOO_PAYMENT_CONFIRM
    else:
        rr = "Якщо виникли питання з оплатою — пишіть, я допоможу."
        await typing_simulation(update, rr)
        save_user_state(str(update.effective_user.id), STAGE_ZOO_PAYMENT, context.user_data)
        return STAGE_ZOO_PAYMENT

async def zoo_payment_confirm_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    await typing_simulation(update, 
        "Дякую за бронювання! Ваше місце офіційно заброньоване. "
        "Скоро надішлю повну інформацію. Якщо будуть питання — звертайтесь!"
    )
    return ConversationHandler.END

# -----------------------------
# CANCEL
# -----------------------------
async def cancel_command(update:Update, context:ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    t = "Добре, завершуємо розмову. Якщо виникнуть питання, звертайтесь знову!"
    await typing_simulation(update, t)
    return ConversationHandler.END

# -----------------------------
# GLOBAL FALLBACK (GPT)
# -----------------------------
async def global_fallback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    fallback_answer = await gpt_fallback(user_text)
    await typing_simulation(update, fallback_answer)

# -----------------------------
# WEBHOOKS
# -----------------------------
@app.route('/')
def index():
    return "Сервер працює! Бот активний."

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method=="POST":
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

    # ConversationHandler (group=0)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_SCENARIO_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, scenario_choice_handler)
            ],
            # CAMP
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
            # ZOO
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

    # Глобальный fallback - group=1
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, global_fallback_handler),
        group=1
    )

    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()

    loop = asyncio.get_running_loop()
    application.bot_data["loop"] = loop

    logger.info("Bot is online and ready.")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info("Starting Flask on port %s", port)
    app.run(host='0.0.0.0', port=port)

if __name__=="__main__":
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Bot thread started. Now starting Flask...")
    start_flask()
