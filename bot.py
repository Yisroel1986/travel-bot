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
    Забираем все товары (туры) из CRM, используя пагинацию.
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
                        all_items.extend(items)
                        total = data.get("total", len(all_items))
                        per_page = data.get("per_page", limit)
                        current_page = data.get("current_page", page)

                    elif "data" in data and isinstance(data["data"], dict):
                        sub = data["data"]
                        items = sub.get("items", [])
                        all_items.extend(items)
                        total = sub.get("total", len(all_items))
                        per_page = sub.get("per_page", limit)
                        current_page = sub.get("page", page)
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
        "Здавалося, ви зайняті, тому я коротко нагадаю про тур у зоопарк Ньїредьгаза. "
        "Це ідеальний день для вашої дитини — і для вас, щоб відпочити. "
        "Ми організуємо все під ключ, щоб ви могли просто насолоджуватися часом разом із сім’єю. "
        "Діти повертаються щасливі, а батьки задоволені. "
        "Якщо будете готові — дайте знати! 😊"
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
    """
    Отправляет "typing..." и потом сообщение с паузой, зависящей от длины текста.
    """
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(4, max(2, len(text)/70)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

def is_positive_response(text: str) -> bool:
    arr = ["так","добре","да","ок","продовжуємо","розкажіть","готовий","готова","привіт","hello","расскажи","зацікав","зацікавлений"]
    return any(k in text.lower() for k in arr)

def is_negative_response(text: str) -> bool:
    arr = ["не хочу","не можу","нет","ні","не буду","не зараз"]
    return any(k in text.lower() for k in arr)

def analyze_intent(text: str) -> str:
    """
    Примитивная логика intent detection: positive / negative / unclear
    """
    if nlp_uk:
        doc = nlp_uk(text)
        lemmas = [token.lemma_.lower() for token in doc]
        pos = {"так","добре","да","ок","продовжувати","розповісти","готовий","готова","привіт","hello","зацікавити","зацікавлений"}
        neg = {"не","нехочу","неможу","нет","ні","небуду","не зараз"}
        if any(kw in lemmas for kw in pos):
            return "positive"
        if any(kw in lemmas for kw in neg):
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
    """
    Обращение к GPT для fallback-ответов
    """
    if openai is None or not OPENAI_API_KEY:
        return "Вибачте, функція ChatGPT недоступна."
    try:
        # Имитируем, что есть модель gpt-4.5 (фейковая, для демонстрации)
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-4.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Error calling ChatGPT: %s", e)
        return "Вибачте, сталася помилка при генерації відповіді."

def detect_special_cases(text: str) -> str:
    """
    Проверяем текст на ключевые возражения/случаи.
    """
    txt_lower = text.lower()

    # Цена слишком высока
    if any(k in txt_lower for k in ["дорого","завелика","не потягну","задорого","слишком дорого"]):
        return "too_expensive"

    # Неуверенность
    if any(k in txt_lower for k in ["не знаю","не впевнений","можливо пізніше","подумаю","может потом"]):
        return "uncertain"

    # Погода
    if any(k in txt_lower for k in ["погода","дождь","дощ","weather"]):
        return "weather"

    return ""

async def handle_special_case(update: Update, context: ContextTypes.DEFAULT_TYPE, case: str):
    """
    Обрабатываем специальные возражения:
    1. Цена слишком высока
    2. Неуверенность
    3. Погода
    """
    if case == "too_expensive":
        text = (
            "Розумію, що ви хвилюєтесь за вартість. Але в цю ціну входить повний пакет: "
            "трансфер, квитки до зоопарку, страхування та супровід. Ваша дитина отримає море емоцій, "
            "а ви зможете відпочити й не хвилюватись про організаційні моменти. Це ж один день, "
            "який ви запам’ятаєте на все життя!\n\n"
            "Якщо хочете щось дешевше, можемо запропонувати варіант без шопінгу або інші дати з акцією. "
            "Вас цікавить така альтернатива?"
        )
        await typing_simulation(update, text)
        return True

    if case == "uncertain":
        text = (
            "Розумію, що вам треба подумати. Пропоную забронювати місце на 24 години без передоплати, "
            "щоб воно точно залишилось за вами. Місця розбирають дуже швидко, особливо на вихідні. "
            "Хотіли б ви скористатися такою можливістю?"
        )
        await typing_simulation(update, text)
        return True

    if case == "weather":
        text = (
            "Погода може бути різною, але в зоопарку є криті павільйони та багато зон для відпочинку. "
            "Ми моніторимо прогноз і якщо буде сильний дощ, попередимо заздалегідь або запропонуємо "
            "перенести дату. Діти однаково отримують купу вражень, навіть якщо трохи накрапає. "
            "Чи можу я відповісти ще на якісь ваші питання?"
        )
        await typing_simulation(update, text)
        return True

    return False

# ---------------------- FLASK & BOT SETUP ----------------------

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

    # -------------------- CONVERSATION HANDLER --------------------
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
            STAGE_END: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    lambda u, c: c.bot.send_message(
                        chat_id=u.effective_chat.id,
                        text="Дякую! Якщо виникнуть питання — /start."
                    )
                )
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)

    # -------------------- WEBHOOK SETUP --------------------
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

# --------------------- HANDLERS LOGIC ----------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()
    cancel_no_response_job(context)

    stg, dat = load_user_state(user_id)
    if stg is not None and dat is not None:
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
        txt = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром до зоопарку Ньїредьгаза, Угорщина. "
            "Це ідеальна можливість подарувати дитині день щастя, а собі — відпочинок від рутини. "
            "Дозвольте задати кілька уточнюючих питань, щоб надати повну інформацію. Добре?"
        )
        await typing_simulation(update, txt)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

async def greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        # Специальные возражения
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_GREET

    if "продовжити" in txt.lower():
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

    if "почати" in txt.lower() or "заново" in txt.lower():
        context.user_data.clear()
        g = (
            "Вітаю вас! 😊 Почнімо спочатку! Одноденний тур в зоопарк Ньїредьгаза — ідеальний вибір для сімейного відпочинку. "
            "Дозвольте поставити кілька уточнень. Готові?"
        )
        await typing_simulation(update, g)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

    intent = analyze_intent(txt)
    if intent == "positive":
        t = (
            "Чудово! Дякую за вашу зацікавленість! "
            "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        )
        await typing_simulation(update, t)
        save_user_state(user_id, STAGE_DEPARTURE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DEPARTURE
    elif intent == "negative":
        m = (
            "Я можу коротко розповісти про наш тур, якщо зараз вам незручно відповідати на питання. "
            "Буде буквально хвилина, щоб ви зрозуміли основну суть."
        )
        await typing_simulation(update, m)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS

    # Fallback
    fp = (
        "В рамках сценарію тура, клієнт написав: " + txt +
        "\nВідповідай українською мовою, дотримуючись сценарію тура."
    )
    fallback_text = await get_chatgpt_response(fp)
    await typing_simulation(update, fallback_text)
    return STAGE_GREET

async def departure_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_DEPARTURE

    context.user_data["departure"] = txt
    r = (
        "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?\n"
        "Ми часто робимо сімейні бонуси, якщо їдуть двоє або більше дітей!"
    )
    await typing_simulation(update, r)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_TRAVEL_PARTY, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_TRAVEL_PARTY

async def travel_party_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_TRAVEL_PARTY

    if "дит" in txt:
        context.user_data["travel_party"] = "child"
        await typing_simulation(update, "Скільки років вашій дитині?")
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CHILD_AGE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHILD_AGE
    else:
        context.user_data["travel_party"] = "no_child"
        r = (
            "Чудово, ми також пропонуємо цікаві програми для дорослих! "
            "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        )
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

async def child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_CHILD_AGE

    if txt.isdigit():
        context.user_data["child_age"] = txt
        r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

    if any(x in txt.lower() for x in ["детал","вартість","ціна","брон"]):
        context.user_data["child_age"] = "unspecified"
        rr = "Добре, перейдемо далі."
        await typing_simulation(update, rr)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CHOICE

    await typing_simulation(update, "Будь ласка, вкажіть вік дитини або задайте інше питання.")
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CHILD_AGE

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_CHOICE

    if "детал" in txt or "деталі" in txt:
        context.user_data["choice"] = "details"
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)

    elif "вартість" in txt or "ціна" in txt:
        context.user_data["choice"] = "cost"
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)

    elif "брон" in txt:
        context.user_data["choice"] = "booking"
        r = (
            "Я дуже рада, що Ви обрали подорож з нами, це буде дійсно крута поїздка. "
            "Давайте забронюємо місце для вас і вашої дитини. Для цього потрібно внести аванс у розмірі 30% "
            "та надіслати фото паспорта або іншого документу. Після цього я надішлю вам усю необхідну інформацію. "
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

    resp = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
    await typing_simulation(update, resp)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_CHOICE, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CHOICE

async def details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_DETAILS

    choice = context.user_data.get("choice","details")
    prods = fetch_all_products()

    fprods = []
    if any(x in txt for x in ["зоопарк","ніредьгаза","нїредьгаза"]):
        for p in prods:
            n = p.get("name","").lower()
            if "зоопарк" in n or "ніредьгаза" in n:
                fprods.append(p)
    else:
        fprods = prods

    if not fprods:
        tours_info = "Наразі немає актуальних турів у CRM або стався збій."
    else:
        if len(fprods) == 1:
            p = fprods[0]
            pname = p.get("name","No name")
            pprice = p.get("price",0)
            pdesc = p.get("description","")
            if not pdesc:
                pdesc = "Без опису"
            tours_info = f"Тур: {pname}\nЦіна: {pprice}\nОпис: {pdesc}"
        else:
            tours_info = "Знайшли кілька турів:\n"
            for p in fprods:
                pid = p.get("id","?")
                pname = p.get("name","No name")
                pprice = p.get("price",0)
                tours_info += f"- {pname} (ID {pid}), ціна: {pprice}\n"

    if choice == "cost":
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева.\n"
            "Це цілий день, і ввечері ви будете вдома.\n"
            "Вартість туру: 1900 грн з особи (включає трансфер, квитки, страхування).\n\n"
            "За ці гроші ви отримуєте готовий день яскравих емоцій і спогадів, "
            "а ще — абсолютний спокій без зайвих клопотів.\n\n"
            + tours_info
        )
    else:
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева.\n"
            "Тривалість: Цілий день.\n"
            "Транспорт: Комфортабельний автобус.\n"
            "Зоопарк: Більше 500 видів тварин.\n"
            "Вартість: 1900 грн (трансфер, квитки, страхування).\n\n"
            "Уявіть, як ваша дитина в захваті від левів, жирафів і слонів, "
            "а ви можете розслабитися і насолоджуватися часом разом. "
            "До того ж ми робимо зупинку в торговому центрі — можна докупити подарунки чи випити кави.\n\n"
            + tours_info
        )

    await typing_simulation(update, text)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)

    await update.effective_chat.send_message(text="Чи є у вас додаткові запитання щодо програми туру? 😊")
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_ADDITIONAL_QUESTIONS

    time_keys = ["коли виїзд","коли відправлення","час виїзду","коли автобус","коли вирушаємо"]
    if any(k in txt for k in time_keys):
        ans = (
            "Виїзд о 6:00 з Ужгорода, о 6:30 з Мукачева, повертаємось орієнтовно о 20:00.\n"
            "Чи є ще запитання?"
        )
        await typing_simulation(update, ans)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    book_keys = ["бронювати","бронюй","купувати тур","давай бронювати","окей давай бронювати","окей бронюй тур"]
    if any(k in txt for k in book_keys):
        r = (
            "Чудово, переходимо до оформлення бронювання. Я надам вам реквізити для оплати. "
            "До речі, у нас залишилось лише декілька вільних місць, тож краще не відкладати!"
        )
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return await close_deal_handler(update, context)

    no_more = ["немає","все зрозуміло","все ок","досить","спасибі","дякую"]
    if any(k in txt for k in no_more):
        rr = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, rr)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_IMPRESSION, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_IMPRESSION

    s = get_sentiment(txt)
    if s == "negative":
        fp = (
            "Клієнт висловив негативне ставлення: " + txt +
            "\nВідповідай українською мовою, проявляючи емпатію, вибачся та запропонуй допомогу."
        )
        fallback_text = await get_chatgpt_response(fp)
        await typing_simulation(update, fallback_text)
        return STAGE_ADDITIONAL_QUESTIONS

    i = analyze_intent(txt)
    if i == "unclear":
        prompt = (
            "В рамках сценарію тура, клієнт задав нестандартне запитання: " + txt +
            "\nВідповідай українською мовою, дотримуючись сценарію та проявляючи розуміння."
        )
        fb = await get_chatgpt_response(prompt)
        await typing_simulation(update, fb)
        return STAGE_ADDITIONAL_QUESTIONS

    ans = (
        "Гарне запитання! Якщо є ще щось, що вас цікавить, будь ласка, питайте.\n\n"
        "Чи є ще запитання?"
    )
    await typing_simulation(update, ans)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_ADDITIONAL_QUESTIONS

async def impression_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_IMPRESSION

    pos = ["добре","клас","цікаво","відмінно","супер","підходить","так"]
    neg = ["ні","не цікаво","дорого","завелика","надто"]
    if any(k in txt for k in pos):
        r = (
            "Чудово! 🎉 Тоді давайте оформимо бронювання, щоб за вами закріпити місце. "
            "Для цього потрібно внести аванс у розмірі 30% і надіслати фото паспорта або іншого документу. "
            "Після цього я надішлю всі деталі, включно з порадами щодо підготовки та списком речей.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL
    elif any(k in txt for k in neg):
        rr = (
            "Шкода це чути. Можливо, вас зацікавлять інші наші тури чи акційні пропозиції? "
            "Якщо у вас залишилися запитання, я із задоволенням відповім."
        )
        await typing_simulation(update, rr)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        resp = (
            "Дякую за вашу думку! Якщо бажаєте, можемо зафіксувати місце зараз, "
            "або ж я можу розповісти про додаткові можливості (наприклад, VIP-пакет з індивідуальним супроводом). "
            "Як краще?"
        )
        await typing_simulation(update, resp)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_CLOSE_DEAL

    pos = ["приват","моно","оплачу","готов","готова","давайте","скинь реквизиты"]
    if any(k in txt for k in pos):
        r = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Як тільки оплатите, будь ласка, надішліть скріншот для підтвердження. "
            "Після цього я відправлю вам програму поїздки і повний список рекомендацій, "
            "щоб подорож пройшла ідеально!"
        )
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

    neg = ["ні","нет","не буду","не хочу"]
    if any(k in txt for k in neg):
        r2 = (
            "Зрозуміло. Якщо зміните рішення або з’являться додаткові запитання, я буду рада допомогти. "
            "Пам’ятайте, що кількість місць обмежена, тож якщо вирішите пізніше — пишіть, "
            "але може вже не залишитися вільних. Гарного дня!"
        )
        await typing_simulation(update, r2)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END

    if any(x in txt for x in ["детал","вартість","ціна","погода","програма","ще питання"]):
        text = (
            "З радістю відповім! Повернімося до деталей туру. "
            "Можете уточнити, що саме цікавить найбільше?"
        )
        await typing_simulation(update, text)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return STAGE_DETAILS

    r3 = (
        "Гаразд! Ви готові завершити оформлення? Вам зручніше оплатити через ПриватБанк чи MonoBank? "
        "А може, хочете дізнатися про VIP-пакет? 😉"
    )
    await typing_simulation(update, r3)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CLOSE_DEAL

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.lower().strip()
    cancel_no_response_job(context)

    case = detect_special_cases(txt)
    if case:
        handled = await handle_special_case(update, context, case)
        if handled:
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_PAYMENT

    if any(k in txt for k in ["оплатив","відправив","скинув","готово","перевёл"]):
        r = (
            "Дякую! Зараз перевірю надходження. Як тільки побачу оплату, "
            "відправлю вам детальну програму та підсумую всі кроки підготовки. "
            "Якщо будуть додаткові побажання — повідомляйте!"
        )
        await typing_simulation(update, r)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_PAYMENT_CONFIRM, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT_CONFIRM
    else:
        rr = (
            "Якщо виникли труднощі з оплатою, я можу допомогти або запропонувати інші способи. "
            "Можливо, вам потрібна консультація щодо банківського переказу?"
        )
        await typing_simulation(update, rr)
        user_id = str(update.effective_user.id)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

async def payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    r = (
        "Дякую за бронювання! Ваше місце офіційно заброньоване. "
        "Я надішлю вам повну інформацію про поїздку, список речей і кілька порад, "
        "щоб подорож пройшла бездоганно. Якщо виникнуть питання — пишіть, я завжди на зв'язку!"
    )
    await typing_simulation(update, r)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_END, context.user_data)
    return STAGE_END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name if user else "Unknown")
    t = (
        "Добре, завершуємо розмову. Якщо захочете повернутися або виникнуть питання — "
        "просто напишіть /start. Завжди рада допомогти!"
    )
    await typing_simulation(update, t)
    uid = str(update.effective_user.id)
    save_user_state(uid, STAGE_END, context.user_data)
    return ConversationHandler.END

# --------------------- MAIN ----------------------

if __name__ == '__main__':
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Bot thread started. Now starting Flask...")
    start_flask()
