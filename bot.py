import os
import logging
import sys
import psutil
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes
)
import openai
from datetime import datetime, timezone, timedelta
from flask import Flask, request
import asyncio
import threading

# Включаем логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения из .env
load_dotenv()

# Считываем токены из .env
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", 'https://your-app.onrender.com')  # Замените на ваш URL

# Назначаем ключ OpenAI
openai.api_key = OPENAI_API_KEY

# Состояния
(
    STATE_INTRO,
    STATE_TOUR_TYPE,
    STATE_NEEDS_CITY,
    STATE_NEEDS_CHILDREN,
    STATE_CONTACT_INFO,
    STATE_PRESENTATION,
    STATE_ADDITIONAL_QUESTIONS,
    STATE_FEEDBACK,
    STATE_PAYMENT,
    STATE_CLOSE_DEAL,
    STATE_FINISH
) = range(11)

# Глобальная переменная для цикла событий бота
bot_loop = None

def is_bot_already_running():
    current_process = psutil.Process()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if process.info['name'] == current_process.name() and \
           process.info['cmdline'] == current_process.cmdline() and \
           process.info['pid'] != current_process.pid:
                return True
    return False

async def invoke_gpt(stage: str, user_text: str, context_data: dict):
    """
    Вызывает OpenAI ChatCompletion с учётом текущего этапа диалога.
    Возвращает ответ от модели.
    """
    system_prompt = f"""
    Ти — команда експертів: SalesGuru, ObjectionsPsychologist, MarketingHacker.
    Урахуй, що наш цільовий клієнт — мама 28-45 років, цінує сім'ю, шукає безпечний і 
    комфортний тур до зоопарку Ньїредьгаза для дитини. 
    Ми використовуємо жіночий м'який тон, 
    робимо акценти на відпочинку для мами, на дитячій радості, безпеці. 
    Застосовуй FOMO (обмеження місць), соціальні докази, 
    якір цін (інші тури дорожчі, але ми даємо те саме, і навіть більше). 
    Стадія: {stage}.
    Повідомлення від користувача: {user_text}.
    Відповідь повинна починатися з "Відповідь менеджера:" і бути написана українською мовою.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Будь ласка, відповідай відповідно до стадії діалогу."}
    ]
    try:
        response = await openai.ChatCompletion.acreate(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=1000,
            temperature=0.7
        )
        advice_text = response["choices"][0]["message"]["content"]
        return advice_text.strip()
    except Exception as e:
        logger.error(f"Помилка при зверненні до OpenAI: {e}")
        return "Відповідь менеджера: На жаль, наразі я не можу відповісти на ваше запитання. Спробуйте пізніше."

def mention_user(update: Update) -> str:
    """Утиліта для красивого звернення по імені."""
    user = update.effective_user
    if user:
        return user.first_name if user.first_name else "друже"
    return "друже"

# Определение всех обработчиков
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = mention_user(update)
    # Советы от экспертов
    adv = await invoke_gpt("intro", "/start", context.user_data)
    logger.info(f"GPT Experts [INTRO]:\n{adv}")

    text = (
        f"Вітання, {user_name}! Я Марія, ваш віртуальний тур-менеджер. "
        "Дякую, що зацікавились нашою сімейною поїздкою до зоопарку Ньїредьгаза.\n\n"
        "Це ідеальний спосіб подарувати дитині казку, а собі — відпочинок без зайвих турбот.\n"
        "Чи можу я поставити кілька уточнюючих питань, щоб ми підібрали найкращий варіант?"
    )
    await update.message.reply_text(f"Відповідь менеджера: {text}")
    return STATE_INTRO

async def intro_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    # GPT
    adv = await invoke_gpt("intro", user_text, context.user_data)
    logger.info(f"GPT Experts [INTRO]:\n{adv}")

    if any(x in user_text for x in ["так", "да", "ок", "добре", "хочу"]):
        reply_keyboard = [['Одноденний тур', 'Довгий тур']]
        markup = ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True)
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Чудово! Який тип туру вас цікавить?",
            reply_markup=markup
        )
        return STATE_TOUR_TYPE
    else:
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Гаразд. Якщо вирішите дізнатися більше — просто напишіть /start або 'Хочу дізнатися'. "
            "Гарного дня!",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

async def tour_type_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    context.user_data["tour_type"] = user_text

    if "одноденний тур" in user_text:
        # Для одноденних турів сразу переходите к выявлению потребностей
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Скажіть, будь ласка, з якого міста ви б хотіли виїжджати (Ужгород чи Мукачево)?"
        )
        return STATE_NEEDS_CITY
    elif "довгий тур" in user_text:
        # Для длительных туров собираем контактные данные
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Щоб підготувати для вас найкращі умови, будь ласка, надайте свої контактні дані (номер телефону або email)."
        )
        return STATE_CONTACT_INFO
    else:
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Будь ласка, оберіть один із запропонованих варіантів.",
            reply_markup=ReplyKeyboardMarkup(
                [['Одноденний тур', 'Довгий тур']], 
                one_time_keyboard=True, 
                resize_keyboard=True
            )
        )
        return STATE_TOUR_TYPE

async def contact_info_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["contact_info"] = user_text

    await update.message.reply_text(
        "Відповідь менеджера: "
        "Дякую! Тепер скажіть, будь ласка, скільки у вас дітей і якої вікової категорії?"
    )
    return STATE_NEEDS_CHILDREN

async def needs_city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["departure_city"] = user_text

    await update.message.reply_text(
        "Відповідь менеджера: "
        "Скільки у вас дітей і якої вікової категорії?"
    )
    return STATE_NEEDS_CHILDREN

async def needs_children_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["children_info"] = user_text

    # GPT
    adv = await invoke_gpt("needs_children", user_text, context.user_data)
    logger.info(f"GPT Experts [NEEDS_CHILDREN]:\n{adv}")

    await update.message.reply_text(
        "Відповідь менеджера: "
        "Зрозуміла вас. Ви не уявляєте, скільки мам вже змогли перезавантажитись і відпочити "
        "завдяки цій поїздці!\n"
        "Дозвольте розповісти трохи про враження, які чекають саме на вас."
    )
    return STATE_PRESENTATION

async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Презентация: отражение потребностей, озвучивание цены, обоснование преимуществ
    departure_city = context.user_data.get("departure_city", "вашого міста")
    tour_type = context.user_data.get("tour_type", "туру")
    children_info = context.user_data.get("children_info", "")

    # Пример отправки мультимедийного контента
    # Вы можете заменить ссылки на реальные изображения или видео
    media = [
        InputMediaPhoto(media="https://example.com/photo1.jpg", caption="Огляд зоопарку Ньїредьгаза"),
        InputMediaPhoto(media="https://example.com/photo2.jpg", caption="Наші комфортні автобуси")
    ]
    await update.message.reply_media_group(media=media)

    presentation_text = (
        "🔸 *Програма туру*:\n"
        f"  • Виїзд о 2:00 з {departure_city} на комфортному автобусі — м'які сидіння, "
        "зарядки для гаджетів, клімат-контроль.\n"
        "  • Прибуття до зоопарку Ньїредьгаза близько 10:00. Діти в захваті від "
        "шоу морських котиків, а ви можете відпочити та зробити купу фото.\n"
        "  • Далі — обід (не входить у вартість, але можна взяти з собою або купити в кафе).\n"
        "  • Після зоопарку — заїзд до великого торгового центру: кава, покупки, відпочинок.\n"
        "  • Повернення додому близько 21:00.\n\n"
        
        "🔸 *Чому це вигідно*:\n"
        "  • Звичайні тури можуть коштувати 2500–3000 грн, і це без гарантій з квитками та "
        "дитячими розвагами. У нас лише 1900 грн (для дорослих), "
        "і 1850 для дітей — вже з квитками, страховкою, супроводом.\n"
        "  • Ми знаємо, що для мами важливо мінімум турбот. Тому все продумано: "
        "діти зайняті, а ви — відпочиваєте!\n\n"
        "🔸 *Місця обмежені*: У нас залишається лише кілька вільних місць на найближчі дати.\n\n"
        "Чи є у вас сумніви або питання? Напишіть, і я з радістю відповім!"
    )

    await update.message.reply_text(
        f"Відповідь менеджера: {presentation_text}",
        parse_mode='Markdown'
    )
    return STATE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt("additional_questions", user_text, context.user_data)
    logger.info(f"GPT Experts [ADDITIONAL_QUESTIONS]:\n{adv}")

    if any(x in user_text for x in ["так", "да", "хочу", "ще питання", "допомога"]):
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Звісно, я готова відповісти на ваші запитання. Що саме вас цікавить?"
        )
        return STATE_ADDITIONAL_QUESTIONS
    else:
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Чудово! Тоді давайте перевіримо, чи готові ви до бронювання місця. "
            "Хочете забронювати місце на найближчу дату?"
        )
        return STATE_FEEDBACK

async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt("feedback", user_text, context.user_data)
    logger.info(f"GPT Experts [FEEDBACK]:\n{adv}")

    if any(x in user_text for x in ["так", "хочу", "бронюю"]):
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Чудово! Для бронювання потрібно внести передоплату 30%. "
            "Ви готові зробити це зараз?"
        )
        return STATE_PAYMENT
    elif any(x in user_text for x in ["ні", "не зараз", "подумаю"]):
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Розумію. Можливо, ви хочете зарезервувати місце без оплати? "
            "Ми можемо тримати його для вас 24 години."
        )
        return STATE_CLOSE_DEAL
    else:
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Вибачте, я не зовсім зрозуміла вашу відповідь. "
            "Ви хочете забронювати місце зараз чи, можливо, потрібно більше часу на роздуми?"
        )
        return STATE_FEEDBACK

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt("payment", user_text, context.user_data)
    logger.info(f"GPT Experts [PAYMENT]:\n{adv}")

    if any(x in user_text for x in ["так", "готовий", "як оплатити"]):
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Чудово! Ось наші реквізити для оплати:\n"
            "[Тут будуть реквізити]\n\n"
            "Після оплати, будь ласка, надішліть скріншот чеку. "
            "Як тільки ми отримаємо підтвердження, я передам вас живому менеджеру "
            "для завершення бронювання. Дякую за довіру!"
        )
        return STATE_CLOSE_DEAL
    else:
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Зрозуміло. Якщо вам потрібен час на роздуми, ми можемо зарезервувати місце на 24 години без оплати. "
            "Хочете скористатися цією можливістю?"
        )
        return STATE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt("close_deal", user_text, context.user_data)
    logger.info(f"GPT Experts [CLOSE_DEAL]:\n{adv}")

    if any(x in user_text for x in ["так", "хочу", "резервую"]):
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Чудово! Я зарезервувала для вас місце на 24 години. "
            "Протягом цього часу ви можете повернутися та завершити бронювання. "
            "Якщо у вас виникнуть додаткові питання, не соромтеся звертатися. "
            "Дякую за інтерес до нашого туру!",
            reply_markup=ReplyKeyboardRemove()
        )
        return STATE_FINISH
    elif any(x in user_text for x in ["ні", "не зараз", "подумаю"]):
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Зрозуміло. Якщо ви передумаєте або у вас виникнуть додаткові питання, "
            "будь ласка, не соромтеся звертатися. Ми завжди раді допомогти!",
            reply_markup=ReplyKeyboardRemove()
        )
        return STATE_FINISH
    else:
        await update.message.reply_text(
            "Відповідь менеджера: "
            "Вибачте, я не зовсім зрозуміла вашу відповідь. "
            "Ви хочете зарезервувати місце зараз чи, можливо, потрібно більше часу на роздуми?"
        )
        return STATE_CLOSE_DEAL

async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt("finish", user_text, context.user_data)
    logger.info(f"GPT Experts [FINISH]:\n{adv}")

    await update.message.reply_text(
        "Відповідь менеджера: "
        "Дякую за спілкування! Якщо у вас виникнуть додаткові питання або ви захочете повернутися "
        "до бронювання, просто напишіть мені. Бажаю гарного дня!",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет и завершает разговор."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        "Відповідь менеджера: "
        'Дякую за спілкування! Якщо захочете повернутися до бронювання, просто напишіть /start.',
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

# Создаем Flask приложение
app = Flask(__name__)

@app.route('/')
def index():
    return "Бот працює!"

@app.route('/webhook', methods=['POST'])
def webhook():
    if request.method == "POST":
        data = request.get_json(force=True)
        update = Update.de_json(data, application.bot)
        # Передаем обновление боту асинхронно
        if bot_loop:
            asyncio.run_coroutine_threadsafe(application.process_update(update), bot_loop)
            logger.info("Webhook отримано та передано боту.")
        else:
            logger.error("Цикл подій бота не ініціалізовано.")
    return "OK"

async def setup_webhook(url, application):
    webhook_url = f"{url}/webhook"
    await application.bot.set_webhook(webhook_url)
    logger.info(f"Webhook встановлено на: {webhook_url}")

async def run_bot():
    global application, bot_loop
    if is_bot_already_running():
        logger.error("Інша інстанція бота вже запущена. Вихід.")
        sys.exit(1)

    # Вказуємо часовий пояс
    tz = timezone(timedelta(hours=2))  # UTC+2 для Києва

    # Логуємо використаний часовий пояс
    logger.info(f"Використаний часовий пояс: {tz}")

    # Створюємо Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Встановлюємо часовий пояс у bot_data
    application.bot_data["timezone"] = tz

    # Створюємо ConversationHandler та додаємо його в додаток
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STATE_INTRO: [MessageHandler(filters.TEXT & ~filters.COMMAND, intro_handler)],
            STATE_TOUR_TYPE: [MessageHandler(filters.Regex('^(Одноденний тур|Довгий тур)$'), tour_type_handler)],
            STATE_NEEDS_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, needs_city_handler)],
            STATE_CONTACT_INFO: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_info_handler)],
            STATE_NEEDS_CHILDREN: [MessageHandler(filters.TEXT & ~filters.COMMAND, needs_children_handler)],
            STATE_PRESENTATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, presentation_handler)],
            STATE_ADDITIONAL_QUESTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, additional_questions_handler)],
            STATE_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler)],
            STATE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handler)],
            STATE_CLOSE_DEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, close_deal_handler)],
            STATE_FINISH: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(conv_handler)

    # Налаштовуємо webhook
    await setup_webhook(WEBHOOK_URL, application)

    # Ініціалізуємо та запускаємо додаток
    await application.initialize()
    await application.start()

    # Отримуємо поточний цикл подій та зберігаємо його в глобальній змінній
    bot_loop = asyncio.get_running_loop()

    # Бот готовий до обробки вебхуків
    logger.info("Telegram бот запущено і готовий до обробки вебхуків.")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Запускаємо Flask на порті {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    # Запускаємо Telegram бота в окремому потоці
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Запуск бота в окремому потоці.")

    # Запускаємо Flask сервер в основному потоці
    start_flask()
