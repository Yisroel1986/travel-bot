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

from deep_translator import GoogleTranslator  # Для auto-detect языка

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

# ЭТАПЫ (по вашему сценарию)
(
    STAGE_GREET,  # Етап 1: Вітання
    STAGE_NO_RESPONSE,  # Етап 2: Якщо клієнт не хоче відповідати/нет ответа
    STAGE_DETAILS,  # Етап 3: Деталі туру
    STAGE_QUESTIONS,  # Етап 4: Додаткові питання
    STAGE_IMPRESSION,  # Етап 5: Запит про загальне враження
    STAGE_CLOSE_DEAL,  # Етап 6: Закриття угоди
    STAGE_PAYMENT,  # Этап 7 (бронювання)
    STAGE_PAYMENT_CONFIRM,  # Этап 8: Підтвердження оплати
    STAGE_END
) = range(9)

# Flask app
app = Flask(__name__)
application = None  # Глобальная переменная для Telegram Application

# DB
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

def save_user_state(user_id: str, stage: int, user_data: dict):
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    data_json = json.dumps(user_data, ensure_ascii=False)
    now = datetime.now().isoformat()
    c.execute("""
        INSERT OR REPLACE INTO conversation_state (user_id, current_stage, user_data, last_interaction)
        VALUES (?, ?, ?, ?)
    """, (user_id, stage, data_json, now))
    conn.commit()
    conn.close()

#
# --- HELPERS ---
#
async def typing_simulation(update: Update, text: str):
    """ Показываем 'набор сообщения', потом отправляем текст. """
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(2, max(1, len(text)/80)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

def mention_user(update: Update) -> str:
    user = update.effective_user
    return user.first_name if user and user.first_name else "друже"

def translate_to_ukrainian(text: str) -> str:
    """Определяем язык пользователя и переводим на украинский, чтобы иметь возможность искать ключевые слова."""
    try:
        # Автоопределение -> перевод в украинский
        translator = GoogleTranslator(source='auto', target='uk')
        result = translator.translate(text)
        return result.lower()
    except Exception as e:
        logger.error("Translation error: %s", e)
        # Если возникла ошибка, вернём исходную строку в нижнем регистре
        return text.lower()

#
# --- KEYWORD DETECTION ---
#
def is_affirmative(ua_text: str) -> bool:
    """Проверяем 'так', 'добре', 'звісно' и т.п. (уже на украинском)."""
    keywords = ["так", "добре", "звісно", "звичайно", "продовжуємо", "розкажіть",
                "починаємо", "готовий", "готова", "га", "давай", "таке", "ага"]
    return any(k in ua_text for k in keywords)

def is_negative(ua_text: str) -> bool:
    """Проверяем 'ні', 'не хочу' и т.п. (уже на украинском)."""
    keywords = ["ні ", "ні.", "ні!", "не хочу", "не можу", "не буду",
                "не готовий", "не готова", "відмовляюся"]
    return any(k in ua_text for k in keywords)

def contains_price_request(ua_text: str) -> bool:
    """Проверяем, спрашивает ли пользователь про цену."""
    # Например: "вартість", "ціна"
    words = ["вартість", "ціна"]
    return any(w in ua_text for w in words)

def contains_details_request(ua_text: str) -> bool:
    """Проверяем, спрашивает ли пользователь 'деталі'."""
    # Например: "деталі", "розкажи докладно", "докладніше", ...
    words = ["деталі", "докладніше"]
    return any(w in ua_text for w in words)

def contains_booking_request(ua_text: str) -> bool:
    """Проверяем, упоминает ли пользователь бронь, оплату."""
    # Например: "бронювати", "забронювати", "оплатити", "хочу оплатити"
    words = ["броню", "заброню", "оплат", "купити"]
    return any(w in ua_text for w in words)

def user_says_no_questions(ua_text: str) -> bool:
    """Пользователь говорит, что нет вопросов."""
    # например: "немає питань", "все ясно", "все зрозуміло"
    words = ["немає", "нема питань", "все ясно", "все зрозуміло", "все ок", "дякую"]
    return any(w in ua_text for w in words)

#
# --- SCENE HANDLERS ---
#
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    init_db()

    saved_stage, saved_data = load_user_state(user_id)
    if saved_stage is not None and saved_data is not None:
        # Предлагаем продолжить или начать заново
        text = (
            "Ви маєте незавершену розмову. Бажаєте продовжити з того ж місця "
            "чи почати заново?\n\nВведіть 'Продовжити' або 'Почати заново'."
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET
    else:
        user_name = mention_user(update)
        greeting_text = (
            f"Вітаю вас, {user_name}! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, greeting_text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET

async def greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    if "продовжити" in ua_text:
        # Подгружаем старую стадию
        old_stage, old_data = load_user_state(user_id)
        if old_stage is not None:
            # возвращаемся туда
            context.user_data.update(json.loads(old_data))
            response_text = "Повертаємось до попередньої розмови."
            await typing_simulation(update, response_text)
            return old_stage
        else:
            # нет данных, начинаем заново
            response_text = "Немає попередніх даних, почнімо з нуля."
            await typing_simulation(update, response_text)
            save_user_state(user_id, STAGE_DETAILS, context.user_data)
            return STAGE_DETAILS

    if "почати" in ua_text or "заново" in ua_text:
        context.user_data.clear()
        greeting_text = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, greeting_text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET

    if is_affirmative(ua_text):
        # "СЦЕНАРІЙ №2 - клієнт відповідає позитивно"
        text = (
            "Дякую за вашу згоду! 😊\n"
            "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌\n"
            "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return STAGE_DETAILS
    elif is_negative(ua_text):
        # "ВАРІАНТ РОЗВИТКУ ПОДІЙ №1" - клиент не хочет отвечать
        short_tour_text = (
            "Я можу коротко розповісти про наш одноденний тур до зоопарку Ньїредьгази, Угорщина. "
            "Це шанс подарувати вашій дитині незабутній день серед екзотичних тварин і водночас нарешті відпочити вам. 🦁🐧 "
            "Ми все організуємо так, щоб ви могли просто насолоджуватися моментами.\n\n"
            "Комфортний автобус, насичена програма і мінімум турбот для вас – все організовано. "
            "Діти отримають море вражень, а ви зможете просто насолоджуватись разом з ними. 🎉\n"
            "Кожен раз наші клієнти повертаються із своїми дітлахами максимально щасливими. "
            "Ви точно полюбите цей тур! 😊\n\n"
            "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева.\n"
            "Тривалість: Цілий день, ввечері Ви вже вдома.\n"
            "Транспорт: Комфортабельний автобус із клімат-контролем.\n"
            "Зоопарк: Більше 500 видів тварин, шоу морських котиків, фото та багато вражень! 🦁\n"
            "Харчування: За власний рахунок, але є час на обід.\n"
            "Додаткові розваги: Після зоопарку — великий торговий центр.\n"
            "Вартість: 1900 грн з особи.\n\n"
            "Чи є у вас запитання?"
        )
        await typing_simulation(update, short_tour_text)
        save_user_state(user_id, STAGE_NO_RESPONSE, context.user_data)
        return STAGE_NO_RESPONSE
    else:
        text = (
            "Вибачте, я не зрозуміла вашу відповідь. "
            "Ви зацікавлені дізнатися деталі туру чи можемо відкласти розмову?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET

async def no_response_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    if is_affirmative(ua_text):
        # Если теперь клиент соглашается
        text = (
            "Чудово! Тоді перейдемо до деталей. "
            "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева, цілий день. "
            "Вартість 1900 грн, включає трансфер, квитки, страховку та супровід.\n\n"
            "Чи є у вас додаткові питання?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_QUESTIONS, context.user_data)
        return STAGE_QUESTIONS
    else:
        text = "Добре, якщо з'являться запитання — пишіть, я на зв'язку!"
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END

async def details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    # Если упомянул "вартість"
    if contains_price_request(ua_text):
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева. 🌟\n"
            "Вартість туру становить 1900 грн з особи. Це ціна, що включає все: "
            "трансфер, квитки, страхування і супровід. Ви платите один раз і більше "
            "не турбуєтеся про організацію! 🏷️\n\n"
            "Чи є у вас додаткові запитання щодо туру?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_QUESTIONS, context.user_data)
        return STAGE_QUESTIONS

    # Если "деталі"
    elif contains_details_request(ua_text):
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода та Мукачева. Цілий день пригод, увечері вже вдома. 🌿\n"
            "Транспорт: комфортабельний автобус, клімат-контроль, зарядки для гаджетів.\n"
            "Зоопарк: понад 500 видів тварин, шоу морських котиків, фото, враження!\n"
            "Харчування: самостійно, але є час на обід у кафе.\n"
            "Після зоопарку: великий торговий центр для відпочинку чи покупок.\n"
            "Вартість: 1900 грн (трансфер, квитки, страховка, супровід).\n\n"
            "Чи є у вас додаткові запитання?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_QUESTIONS, context.user_data)
        return STAGE_QUESTIONS

    # Если "оплатить" или "забронировать"
    elif contains_booking_request(ua_text):
        text = (
            "Я дуже рада, що Ви обрали подорож з нами, це буде дійсно крута поїздка. "
            "Давайте забронюємо місце для вас і вашої дитини.\n\n"
            "Для цього потрібно внести аванс у розмірі 30% та надіслати фото паспорта. "
            "Після цього я надішлю вам усю необхідну інформацію для підготовки.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        return STAGE_PAYMENT
    else:
        # Иначе просто задаём уточнения
        text = (
            "Для кого ви плануєте цю поїздку? Скільки років вашій дитині?\n\n"
            "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_QUESTIONS, context.user_data)
        return STAGE_QUESTIONS

async def questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    # Если нет вопросов
    if user_says_no_questions(ua_text):
        text = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_IMPRESSION, context.user_data)
        return STAGE_IMPRESSION
    else:
        # Пример: если спрашивает про "дитина злякається"
        if "дитина" in ua_text and "зляка" in ua_text:
            answer_text = (
                "Розумію ваші хвилювання. Ми організовуємо екскурсію так, щоб діти почувалися максимально комфортно. "
                "У зоопарку є зони відпочинку, дитячі майданчики, шоу морських котиків. "
                "Програма орієнтована на дітей, тому хвилюватися не варто!"
            )
        elif "потрібно подумати" in ua_text:
            answer_text = (
                "Розумію, що рішення важливе. Ми можемо зарезервувати місце на 24 години без передоплати, "
                "щоб ви мали час ухвалити рішення. Місця обмежені!"
            )
        else:
            answer_text = (
                "Дякую за запитання! Ми завжди раді допомогти. Якщо у вас є особливі побажання "
                "або додаткові питання — повідомте, будь ласка."
            )

        await typing_simulation(update, answer_text + "\n\nЧи є ще запитання?")
        save_user_state(user_id, STAGE_QUESTIONS, context.user_data)
        return STAGE_QUESTIONS

async def impression_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    # Если клиент отвечает позитивно -> закрываем сделку
    if is_affirmative(ua_text):
        text = (
            "Чудово! 🎉 Давайте забронюємо місце для вас і вашої дитини, щоб забезпечити комфортний відпочинок. "
            "Для цього потрібно внести аванс 30% і надіслати фото паспорта. "
            "Після цього я відправлю всю інформацію для підготовки.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank?"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return STAGE_CLOSE_DEAL
    elif is_negative(ua_text):
        text = (
            "Шкода це чути. Якщо у вас лишилися питання або хочете розглянути інші варіанти, "
            "напишіть, будь ласка. Ми завжди на зв'язку!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        # Если говорит "нужно подумать" etc.
        if "потрібно подумати" in ua_text:
            text = (
                "Розумію. Ми можемо тримати місце 24 години без передоплати, "
                "щоб ви мали час все обдумати. Місця швидко розкуповують!"
            )
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_END, context.user_data)
            return STAGE_END
        else:
            text = (
                "Вибачте, я не впевнена, чи готові ви до бронювання. "
                "Якщо так — скажіть, і я оформлю бронь! Якщо ні — можемо відкласти."
            )
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
            return STAGE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    # Проверяем, не подтверждает ли оплату
    if contains_booking_request(ua_text) or is_affirmative(ua_text):
        text = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Після оплати надішліть, будь ласка, скрін, і я одразу підтверджу бронювання!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        return STAGE_PAYMENT
    elif is_negative(ua_text):
        text = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        if "альтернатив" in ua_text or "інший" in ua_text:
            text = (
                "Звичайно, у нас є інші варіанти турів і дат. "
                "Які саме побажання у вас є? Можемо щось обрати!"
            )
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
            return STAGE_CLOSE_DEAL
        else:
            text = (
                "Перепрошую, не зовсім зрозуміла. Ви хочете оформити бронь чи ще маєте сумніви?"
            )
            await typing_simulation(update, text)
            save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
            return STAGE_CLOSE_DEAL

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    # Допустим, пользователь сообщил "оплатив" / "готово"
    if "оплатив" in ua_text or "готово" in ua_text or "відправив" in ua_text:
        text = (
            "Дякую! Зараз перевірю. Як все буде добре, надішлю підтвердження і деталі поїздки!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT_CONFIRM, context.user_data)
        return STAGE_PAYMENT_CONFIRM
    else:
        text = (
            "Якщо у вас виникли питання щодо оплати або потрібна допомога — напишіть. "
            "Чекаю на підтвердження!"
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        return STAGE_PAYMENT

async def payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_text_original = update.message.text
    ua_text = translate_to_ukrainian(user_text_original)

    text = (
        "Дякую за бронювання! 🎉 Ми успішно зберегли за вами місце в турі до зоопарку Ньїредьгаза. "
        "Найближчим часом я надішлю список речей, час виїзду та всі деталі. "
        "Якщо у вас виникнуть питання — звертайтеся, завжди на зв'язку!"
    )
    await typing_simulation(update, text)
    save_user_state(user_id, STAGE_END, context.user_data)
    return STAGE_END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    logger.info("User %s canceled.", user.first_name if user else "Unknown")
    text = "Гаразд, тоді завершуємо розмову. Якщо що — пишіть!"
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
        global application
        if not application:
            logger.error("Application is not initialized yet.")
            return "No application available"

        data = request.get_json(force=True)
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

    from telegram.ext import ApplicationBuilder

    logger.info("Starting bot...")

    request = HTTPXRequest(connect_timeout=20, read_timeout=40)
    builder = ApplicationBuilder().token(BOT_TOKEN).request(request)
    global application
    application = builder.build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STAGE_GREET: [MessageHandler(filters.TEXT & ~filters.COMMAND, greet_handler)],
            STAGE_NO_RESPONSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, no_response_handler)],
            STAGE_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, details_handler)],
            STAGE_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, questions_handler)],
            STAGE_IMPRESSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, impression_handler)],
            STAGE_CLOSE_DEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, close_deal_handler)],
            STAGE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handler)],
            STAGE_PAYMENT_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_confirm_handler)],
            STAGE_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               lambda u, c: c.bot.send_message(u.effective_chat.id,
                               "Дякую! Якщо виникнуть питання — /start."))
            ],
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
