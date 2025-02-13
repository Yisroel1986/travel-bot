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
    JobQueue,
    CallbackContext
)
from telegram.request import HTTPXRequest
from datetime import timezone, timedelta, datetime
from flask import Flask, request
import asyncio
import threading

# --- LOGGING AND SETTINGS ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", 'https://your-app.onrender.com')

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

# Состояния диалога (строго по сценарию)
(
    STAGE_GREET,                 # 1. Вітання
    STAGE_NO_RESPONSE_SCENARIO,  # 2. Якщо клієнт не відповідає
    STAGE_DETAILS,               # 3. Деталі туру
    STAGE_ADDITIONAL_QUESTIONS,  # 4. Додаткові питання
    STAGE_IMPRESSION,            # 5. Запит про загальне враження
    STAGE_CLOSE_DEAL,            # 6. Закриття угоди
    STAGE_PAYMENT,               # 7. Бронювання
    STAGE_PAYMENT_CONFIRM,       # 8. Підтвердження оплати
    STAGE_END                    # Завершення
) = range(9)

# Задержка в секундах для случая «нет ответа» (6 часов)
NO_RESPONSE_DELAY_SECONDS = 6 * 3600

# Инициализация Flask-приложения
app = Flask(__name__)

# Глобально объявляем application, чтобы использовать в webhook()
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
# --- FOLLOW-UP LOGIC (NO RESPONSE) ---
#
def no_response_callback(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    chat_id = context.job.chat_id

    message = (
        "Я можу коротко розповісти про наш одноденний тур до зоопарку Ньїредьгази, Угорщина. "
        "Це шанс подарувати вашій дитині незабутній день серед екзотичних тварин і водночас нарешті відпочити вам. 🦁🐧 "
        "Ми все організуємо так, щоб ви могли просто насолоджуватися моментами.\n\n"
        "Комфортний автобус, насичена програма і мінімум турбот для вас – все організовано. "
        "Діти отримають море вражень, а ви зможете просто насолоджуватись разом з ними. 🎉\n"
        "Кожен раз наші клієнти повертаються із своїми дітлахами максимально щасливими. "
        "Ви точно полюбите цей тур! 😊\n\n"
        "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева.\n"
        "Тривалість: Цілий день, ввечері Ви вже вдома.\n"
        "Транспорт: Комфортабельний автобус із клімат-контролем та зарядками. 🚌\n"
        "Зоопарк: Більше 500 видів тварин, шоу морських котиків, фото та багато вражень! 🦁\n"
        "Харчування: За власний рахунок, але у нас передбачений час для обіду. 🍽️\n"
        "Додаткові розваги: Після відвідування зоопарку — великий торговий центр.\n"
        "Вартість: 1900 грн з особи.\n\n"
        "Чи є у вас запитання? 😊"
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
        data={"message": "Похоже, вы не отвечаете..."}
    )

def cancel_no_response_job(context: CallbackContext):
    job_queue = context.job_queue
    chat_id = context._chat_id  # В v20 можно получить chat_id из контекста
    current_jobs = job_queue.get_jobs_by_name(f"no_response_{chat_id}")
    for job in current_jobs:
        job.schedule_removal()

#
# --- HELPER ---
#
async def typing_simulation(update: Update, text: str):
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(2, max(1, len(text)/80)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

def mention_user(update: Update) -> str:
    user = update.effective_user
    return user.first_name if user and user.first_name else "друже"

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
        user_name = mention_user(update)
        greeting_text = (
            f"Вітаю вас, {user_name}! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, greeting_text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

async def greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if "продовжити" in user_text:
        saved_stage, saved_data_json = load_user_state(user_id)
        if saved_stage is not None:
            context.user_data.update(json.loads(saved_data_json))
            response_text = "Повертаємось до попередньої розмови."
            await typing_simulation(update, response_text)
            schedule_no_response_job(context, update.effective_chat.id)
            return saved_stage
        else:
            response_text = "Немає попередніх даних, почнімо з нуля."
            await typing_simulation(update, response_text)
            save_user_state(user_id, STAGE_DETAILS, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_DETAILS

    if "почати" in user_text or "заново" in user_text:
        context.user_data.clear()
        greeting_text = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, greeting_text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_GREET

    positive_keywords = ["так", "добре", "да", "ок", "продовжуємо", "розкажіть", "готовий", "готова"]
    if any(k in user_text for k in positive_keywords):
        response_text = (
            "Дякую за згоду! Зараз розповім усі деталі туру. "
            "Але спершу хочу переконатися: для кого ви плануєте цю поїздку? "
            "Чи плануєте їхати разом з дитиною?"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS
    else:
        negative_keywords = ["не хочу", "не можу", "нет", "ні", "не буду", "не зараз"]
        if any(k in user_text for k in negative_keywords):
            message = (
                "Я можу коротко розповісти про наш одноденний тур до зоопарку Ньїредьгази, Угорщина. "
                "Це шанс подарувати вашій дитині незабутній день серед екзотичних тварин і водночас нарешті відпочити вам. 🦁🐧 "
                "Ми все організуємо так, щоб ви могли просто насолоджуватися моментами.\n\n"
                "Комфортний автобус, насичена програма і мінімум турбот для вас – все організовано. "
                "Діти отримають море вражень, а ви зможете просто насолоджуватись разом з ними. 🎉\n"
                "Кожен раз наші клієнти повертаються із своїми дітлахами максимально щасливими. "
                "Ви точно полюбите цей тур! 😊\n\n"
                "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева.\n"
                "Тривалість: Цілий день, ввечері Ви вже вдома.\n"
                "Транспорт: Комфортабельний автобус із клімат-контролем та зарядками. 🚌\n"
                "Зоопарк: Більше 500 видів тварин, шоу морських котиків, фото та багато вражень! 🦁\n"
                "Харчування: За власний рахунок, але у нас передбачений час для обіду. 🍽️\n"
                "Додаткові розваги: Після зоопарку — великий торговий центр.\n"
                "Вартість: 1900 грн з особи.\n\n"
                "Чи є у вас питання?"
            )
            await typing_simulation(update, message)
            save_user_state(user_id, STAGE_NO_RESPONSE_SCENARIO, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_NO_RESPONSE_SCENARIO
        else:
            text = (
                "Вибачте, я не зрозуміла вашу відповідь. "
                "Ви зацікавлені дізнатися деталі туру чи можемо відкласти розмову?"
            )
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_GREET, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_GREET

async def no_response_scenario_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    positive_keywords = ["так", "добре", "да", "ок", "продовжуємо", "розкажіть"]
    if any(k in user_text for k in positive_keywords):
        response_text = (
            "Чудово! Тоді давайте перейдемо до деталей.\n"
            "Для кого ви розглядаєте цю поїздку? Плануєте їхати з дитиною?"
        )
        await typing_simulation(update, response_text)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_DETAILS
    else:
        negative_keywords = ["ні", "нет", "не хочу", "не буду", "пізніше"]
        if any(k in user_text for k in negative_keywords):
            text = "Добре, я буду на зв'язку, якщо передумаєте."
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_END, context.user_data)
            return STAGE_END
        else:
            text = "Можемо переходити до деталей туру чи вам потрібно більше часу?"
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_NO_RESPONSE_SCENARIO, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_NO_RESPONSE_SCENARIO

async def details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if "вартість" in user_text or "ціна" in user_text:
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева. 🌟\n"
            "Це цілий день, наповнений пригодами, і вже ввечері ви будете вдома. "
            "Уявіть, як ваша дитина в захваті від зустрічі з левами, слонами і жирафами, "
            "а ви можете насолодитися спокійним часом на природі без зайвих турбот.\n\n"
            "Вартість туру становить 1900 грн з особи. Це ціна, що включає все — "
            "трансфер, квитки до зоопарку, страхування та супровід. "
            "Ви платите один раз і більше не турбуєтеся про жодні організаційні моменти! 🏷️\n\n"
            "Подорож на комфортабельному автобусі із зарядками і клімат-контролем. 🚌 "
            "Наш супровід вирішує всі організаційні питання. Діти будуть щасливі, а ви зможете відпочити!\n\n"
            "Чи є у вас додаткові запитання?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    elif "детал" in user_text:
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева. 🌟\n"
            "Це цілий день, наповнений пригодами, і вже ввечері ви будете вдома, "
            "сповнені приємних спогадів.\n\n"
            "Транспорт: Комфортабельний автобус (клімат-контроль, зарядки). 🚌\n"
            "Зоопарк: Понад 500 видів тварин, шоу морських котиків, фото і море вражень! 🦁\n"
            "Харчування: Самостійно, але передбачено час на обід у затишному кафе.\n"
            "Додаткові розваги: Після зоопарку — торговий центр.\n"
            "Вартість туру: 1900 грн (трансфер, квитки, страховка, супровід).\n\n"
            "Чи є у вас додаткові запитання?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

    elif "броню" in user_text:
        text = (
            "Я дуже рада, що Ви обрали подорож з нами, це буде дійсно крута поїздка. "
            "Давайте забронюємо місце для вас і вашої дитини.\n\n"
            "Для цього потрібно внести аванс у розмірі 30% та надіслати фото паспорта чи іншого документа. "
            "Після цього я надішлю вам усю необхідну інформацію.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT
    else:
        text = (
            "Добре! Скажіть, будь ласка, звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? "
            "І чи їдете ви з дитиною?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    no_more_questions = ["немає", "все зрозуміло", "все ок", "досить", "спасибі", "дякую"]
    if any(k in user_text for k in no_more_questions):
        text = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_IMPRESSION, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_IMPRESSION
    else:
        if "дитина" in user_text and "злякається" in user_text:
            answer_text = (
                "Розумію ваші хвилювання. Ми організовуємо все так, щоб діти почувалися максимально комфортно: "
                "зони відпочинку, дитячі майданчики, шоу морських котиків. Програма адаптована для малечі!"
            )
        elif "потрібно подумати" in user_text or "вагаюся" in user_text:
            answer_text = (
                "Розумію, що рішення важливе. Ми можемо зарезервувати місце на 24 години без передоплати, "
                "щоб ви мали час ухвалити рішення. Місця обмежені!"
            )
        else:
            answer_text = (
                "Гарне запитання! Ми надаємо всі додаткові послуги, дбаємо про комфорт і безпеку. "
                "Будь ласка, пишіть, якщо виникнуть інші уточнення."
            )

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
        text = (
            "Чудово! 🎉 Давайте забронюємо місце для вас і вашої дитини, щоб забезпечити комфорт. "
            "Ми все організуємо, а вам залишиться лише насолоджуватися днем.\n\n"
            "Для цього потрібно внести аванс у розмірі 30% та надіслати фото паспорта. "
            "Після цього надішлю детальну інформацію.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL
    elif any(k in user_text for k in negative_keywords):
        text = (
            "Шкода це чути. Якщо у вас лишилися питання або хочете розглянути інші варіанти — "
            "повідомте, будь ласка. Ми завжди на зв'язку!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        if "потрібно подумати" in user_text or "вагаюся" in user_text:
            text = (
                "Розумію, що рішення важливе. Ми можемо зарезервувати місце без передоплати на 24 години, "
                "щоб ви мали час. Місця обмежені."
            )
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_END, context.user_data)
            return STAGE_END
        else:
            text = "Дякую за думку! Чи готові ви переходити до бронювання?"
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
            schedule_no_response_job(context, update.effective_chat.id)
            return STAGE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    positive_keywords = ["приват", "моно", "оплачу", "готов", "готова", "давайте"]
    if any(k in user_text for k in positive_keywords):
        text = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Як оплатите, надішліть скріншот — одразу підтверджу бронювання!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

    negative_keywords = ["ні", "нет", "не буду", "не хочу"]
    if any(k in user_text for k in negative_keywords):
        text = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END

    if "альтернатив" in user_text or "інш" in user_text:
        text = (
            "Звичайно! У нас є інші варіанти турів. "
            "Можемо запропонувати іншу дату або іншу програму. Що саме вам цікаво?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_CLOSE_DEAL

    text = (
        "Дякую! Ви готові завершити оформлення? "
        "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
    )
    await typing_simulation(update, text)
    save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
    schedule_no_response_job(context, update.effective_chat.id)
    return STAGE_CLOSE_DEAL

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    if "оплатив" in user_text or "відправив" in user_text or "скинув" in user_text or "готово" in user_text:
        text = (
            "Дякую! Тепер перевірю надходження. Як тільки все буде ок, "
            "я надішлю деталі поїздки і підтвердження бронювання!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT_CONFIRM, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT_CONFIRM
    else:
        text = "Якщо виникли додаткові питання — я на зв'язку. Потрібна допомога з оплатою?"
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        schedule_no_response_job(context, update.effective_chat.id)
        return STAGE_PAYMENT

async def payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text = update.message.text.lower().strip()
    cancel_no_response_job(context)

    text = (
        "Дякую за бронювання! 🎉 Ми успішно зберегли за вами місце в турі до зоопарку Ньїредьгаза. "
        "Найближчим часом я надішлю всі деталі (список речей, час виїзду тощо). Якщо є питання, "
        "звертайтеся. Ми завжди на зв'язку!"
    )
    await typing_simulation(update, text)
    save_user_state(user_id, STAGE_END, context.user_data)
    return STAGE_END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cancel_no_response_job(context)
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name if user else "Unknown")
    text = (
        "Гаразд, завершуємо розмову. Якщо виникнуть питання, "
        "завжди можете звернутися знову!"
    )
    await typing_simulation(update, text)
    user_id = str(update.effective_user.id)
    save_user_state(user_id, STAGE_END, context.user_data)
    return ConversationHandler.END

#
# --- WEBHOOK & BOT LAUNCH ---
#
@app.route('/')
def index():
    return "Сервер працює! Бот активний."

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        # ВАЖНО: используем глобальную переменную application
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

    request = HTTPXRequest(connect_timeout=20, read_timeout=40)
    application_builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
    )
    # Делаем глобальную
    global application
    application = application_builder.build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_GREET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, greet_handler)
            ],
            STAGE_NO_RESPONSE_SCENARIO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, no_response_scenario_handler)
            ],
            STAGE_DETAILS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, details_handler)
            ],
            STAGE_ADDITIONAL_QUESTIONS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, additional_questions_handler)
            ],
            STAGE_IMPRESSION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, impression_handler)
            ],
            STAGE_CLOSE_DEAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, close_deal_handler)
            ],
            STAGE_PAYMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handler)
            ],
            STAGE_PAYMENT_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, payment_confirm_handler)
            ],
            STAGE_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               # Важно: (update, context) сигнатура
                               lambda update, context: context.bot.send_message(
                                   chat_id=update.effective_chat.id,
                                   text="Дякую! Якщо виникнуть питання — /start."
                               ))
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)

    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()

    # Сохраняем loop
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
