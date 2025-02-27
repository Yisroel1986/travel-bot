import os
import logging
import sys
import psutil
import sqlite3
import json
from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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
    sentiment_pipeline = pipeline(
        "sentiment-analysis",
        model="nlptown/bert-base-multilingual-uncased-sentiment"
    )
except:
    sentiment_pipeline = None

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_raw_crm_api_key = os.getenv("CRM_API_KEY","").strip().strip('"')
_raw_crm_api_url = os.getenv("CRM_API_URL","https://openapi.keycrm.app/v1/products").strip().strip('"')
CRM_API_KEY = _raw_crm_api_key
CRM_API_URL = _raw_crm_api_url
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL","https://your-app.onrender.com")

if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

def is_bot_already_running():
    cp=psutil.Process()
    for p in psutil.process_iter(['pid','name','cmdline']):
        if p.info['name']==cp.name() and p.info['cmdline']==cp.cmdline() and p.info['pid']!=cp.pid:
            return True
    return False

(
    STAGE_START,
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
) = range(13)

NO_RESPONSE_DELAY_SECONDS = 6*3600
app = Flask(__name__)
application = None

def init_db():
    conn=sqlite3.connect("bot_database.db")
    c=conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_state(
            user_id TEXT PRIMARY KEY,
            current_stage INTEGER,
            user_data TEXT,
            last_interaction TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def load_user_state(user_id:str):
    conn=sqlite3.connect("bot_database.db")
    c=conn.cursor()
    c.execute("SELECT current_stage,user_data FROM conversation_state WHERE user_id=?",(user_id,))
    row=c.fetchone()
    conn.close()
    if row:return row[0],row[1]
    return None,None

def save_user_state(user_id:str, stage:int, user_data:dict):
    conn=sqlite3.connect("bot_database.db")
    c=conn.cursor()
    js=json.dumps(user_data,ensure_ascii=False)
    now=datetime.now().isoformat()
    c.execute("""
        INSERT OR REPLACE INTO conversation_state(user_id,current_stage,user_data,last_interaction)
        VALUES(?,?,?,?)
    """,(user_id,stage,js,now))
    conn.commit()
    conn.close()

def fetch_all_products():
    if not CRM_API_KEY or not CRM_API_URL:
        return []
    headers={"Authorization":f"Bearer {CRM_API_KEY}","Accept":"application/json"}
    all_items=[]
    page=1
    limit=50
    while True:
        try:
            resp=requests.get(CRM_API_URL,headers=headers,params={"page":page,"limit":limit},timeout=10)
            if resp.status_code!=200:
                break
            data=resp.json()
            if isinstance(data,dict):
                if "data" in data and isinstance(data["data"],list):
                    items=data["data"]
                    all_items.extend(items)
                    total=data.get("total",len(all_items))
                    if len(all_items)>=total:break
                    page+=1
                elif "data" in data and isinstance(data["data"],dict):
                    sub=data["data"]
                    items=sub.get("items",[])
                    all_items.extend(items)
                    total=sub.get("total",len(all_items))
                    if len(all_items)>=total:break
                    page+=1
                else:
                    break
            else:
                break
        except:
            break
    return all_items

def no_response_callback(ctx:ContextTypes.DEFAULT_TYPE):
    cid=ctx.job.chat_id
    msg=(
        "Я можу коротко розповісти про наш одноденний тур..."
    )
    ctx.bot.send_message(chat_id=cid,text=msg)

def schedule_no_response_job(ctx:CallbackContext, chat_id:int):
    jq=ctx.job_queue
    jobs=jq.get_jobs_by_name(f"no_response_{chat_id}")
    for j in jobs:j.schedule_removal()
    jq.run_once(no_response_callback,NO_RESPONSE_DELAY_SECONDS,chat_id=chat_id,name=f"no_response_{chat_id}",data={})

def cancel_no_response_job(ctx:CallbackContext):
    jq=ctx.job_queue
    cid=getattr(ctx,"_chat_id",None)
    if cid:
        jobs=jq.get_jobs_by_name(f"no_response_{cid}")
        for j in jobs:j.schedule_removal()

async def typing_simulation(update:Update,txt:str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(3,max(2,len(txt)/30)))
    await update.message.reply_text(txt,reply_markup=ReplyKeyboardRemove())

def is_positive_response(txt:str)->bool:
    arr=["так","добре","да","ок","продовжуємо","розкажіть","готовий","готова","привіт","hello","расскажи","зацікав","зацікавлений"]
    return any(k in txt.lower() for k in arr)

def is_negative_response(txt:str)->bool:
    arr=["не хочу","не можу","нет","ні","не буду","не зараз"]
    return any(k in txt.lower() for k in arr)

async def get_chatgpt_response(prompt:str)->str:
    return "Пример ответа"  # Здесь упрощаем, чтобы не нагружать. Или max_tokens=512

########################
#   INLINE KEY EXAMPLE
########################

# Пример callback data:
CB_START_OK = "start_ok"
CB_START_CANCEL = "start_cancel"
CB_CHOICE_DETAILS = "cho_details"
CB_CHOICE_PRICE = "cho_price"
CB_CHOICE_BOOKING = "cho_booking"

########################
#   HANDLERS
########################

async def cmd_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard=[
        [InlineKeyboardButton("Підключитися до менеджера", callback_data=CB_START_OK)],
        [InlineKeyboardButton("Скасувати", callback_data=CB_START_CANCEL)]
    ]
    markup=InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Вітаю! Ви можете підключитися до менеджера, або скасувати.",
        reply_markup=markup
    )
    return STAGE_START

async def start_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    await query.answer()
    if query.data==CB_START_OK:
        await query.message.reply_text("Дякую! Починаємо спілкування.")
        return await greet_handler_by_button(query,context)
    elif query.data==CB_START_CANCEL:
        await query.message.reply_text("Скасували. Якщо що, пишіть /start")
        return ConversationHandler.END

async def greet_handler_by_button(query, context):
    user_id=str(query.from_user.id)
    init_db()
    s,u=load_user_state(user_id)
    if s is not None and u is not None:
        t="У вас є незавершена розмова. Продовжити чи почати наново?"
        await query.message.reply_text(t)
        save_user_state(user_id,STAGE_GREET,context.user_data)
        # schedule job
        return STAGE_GREET
    else:
        gr="Вітаю! Ви зацікавилися нашим туром..."
        await query.message.reply_text(gr)
        save_user_state(user_id,STAGE_GREET,context.user_data)
        return STAGE_GREET

# Пример: в choice_handler теперь inline кнопки
async def choice_inline_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    keyboard=[
        [InlineKeyboardButton("Деталі туру", callback_data=CB_CHOICE_DETAILS)],
        [InlineKeyboardButton("Вартість", callback_data=CB_CHOICE_PRICE)],
        [InlineKeyboardButton("Забронювати", callback_data=CB_CHOICE_BOOKING)]
    ]
    markup=InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "Що вас цікавить найбільше?",
        reply_markup=markup
    )
    return STAGE_CHOICE

async def choice_callback_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    await query.answer()
    if query.data==CB_CHOICE_DETAILS:
        context.user_data["choice"]="details"
        await query.message.reply_text("Ви обрали деталі туру.")
        # ... перейти в STAGE_DETAILS, вызвать details_handler etc.
        return STAGE_DETAILS
    elif query.data==CB_CHOICE_PRICE:
        context.user_data["choice"]="cost"
        await query.message.reply_text("Ви обрали вартість.")
        return STAGE_DETAILS
    elif query.data==CB_CHOICE_BOOKING:
        context.user_data["choice"]="booking"
        await query.message.reply_text("Ви обрали бронювання.")
        return STAGE_CLOSE_DEAL

########################
#   FLASK + TELEGRAM
########################

@app.route('/')
def index():
    return "Сервер працює! Бот активний."

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method=="POST":
        data=request.get_json(force=True)
        global application
        if not application:return "No application available"
        upd=Update.de_json(data,application.bot)
        loop=application.bot_data.get("loop")
        if loop:
            asyncio.run_coroutine_threadsafe(application.process_update(upd),loop)
    return "OK"

async def setup_webhook(url, app_ref):
    wh=url+"/webhook"
    await app_ref.bot.set_webhook(wh)

def start_flask():
    p=int(os.environ.get("PORT","10000"))
    app.run(host="0.0.0.0", port=p)

async def run_bot():
    if is_bot_already_running():
        sys.exit(1)
    req=HTTPXRequest(connect_timeout=20,read_timeout=40)
    global application
    ab=Application.builder().token(BOT_TOKEN).request(req)
    application=ab.build()

    conv_handler=ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start_button)
        ],
        states={
            STAGE_START: [
                CallbackQueryHandler(start_callback_handler, pattern=f"^{CB_START_OK}$|^{CB_START_CANCEL}$")
            ],
            STAGE_GREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, ...)],
            # ... Остальные states
            STAGE_CHOICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, choice_inline_handler),
                CallbackQueryHandler(choice_callback_handler, pattern=f"^{CB_CHOICE_DETAILS}$|^{CB_CHOICE_PRICE}$|^{CB_CHOICE_BOOKING}$")
            ],
        },
        fallbacks=[]
    )
    application.add_handler(conv_handler)

    await setup_webhook(WEBHOOK_URL,application)
    await application.initialize()
    await application.start()
    loop=asyncio.get_running_loop()
    application.bot_data["loop"]=loop

if __name__=="__main__":
    bot_thread=threading.Thread(target=lambda:asyncio.run(run_bot()),daemon=True)
    bot_thread.start()
    start_flask()
