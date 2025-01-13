import os
import logging
import sys
import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes
import openai
from datetime import datetime, timezone, timedelta

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

# Назначаем ключ OpenAI
openai.api_key = OPENAI_API_KEY

# Состояния
(
    STATE_INTRO,
    STATE_NEEDS,
    STATE_PSYCHO,
    STATE_PRESENTATION,
    STATE_OBJECTIONS,
    STATE_QUOTE,
    STATE_FAQ,
    STATE_FEEDBACK,
    STATE_PAYMENT,
    STATE_RESERVATION,
    STATE_TRANSFER,
    STATE_FINISH
) = range(12)

def is_bot_already_running():
    current_process = psutil.Process()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if process.info['name'] == current_process.name() and \
           process.info['cmdline'] == current_process.cmdline() and \
           process.info['pid'] != current_process.pid:
            return True
    return False

async def invoke_gpt_experts(stage: str, user_text: str, context_data: dict):
    """
    Вызывает OpenAI ChatCompletion, передавая «ролям-экспертам» текущий этап,
    текст пользователя и контекст.
    Возвращает строку советов. 
    """
    system_prompt = f"""
    Ты — команда экспертов: SalesGuru, ObjectionsPsychologist, MarketingHacker.
    Учти, что наш целевой клиент — мама 28-45 лет, ценящая семью, ищет безопасный и 
    комфортный тур в зоопарк Ньїредьгаза для ребенка. 
    Мы используем женский мягкий тон, 
    делаем акценты на отдыхе для мамы, на детской радости, безопасности. 
    Применяй FOMO (ограничения мест), соцдоказательства, 
    якорение цены (другие туры дороже, но мы даём то же, и даже больше). 
    Стадия: {stage}.
    Сообщение от пользователя: {user_text}.
    Дай 3 коротких совета, по 1-2 предложения, от имени каждой роли.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Пожалуйста, дай три совета для бота (1 от каждого эксперта)."}
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
        logger.error(f"Ошибка при обращении к OpenAI: {e}")
        return "(Не удалось получить советы от виртуальных экспертов)"

def mention_user(update: Update) -> str:
    """Утилита для красивого обращения по имени."""
    user = update.effective_user
    if user:
        return user.first_name if user.first_name else "друже"
    return "друже"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = mention_user(update)
    # Советы от экспертов
    adv = await invoke_gpt_experts("intro", "/start", context.user_data)
    logger.info(f"GPT Experts [INTRO]:\n{adv}")

    text = (
        f"Привіт, {user_name}! Я Марія, ваш віртуальний тур-менеджер. "
        "Дякую, що зацікавились нашою сімейною поїздкою до зоопарку Ньїредьгаза.\n\n"
        "Це ідеальний спосіб подарувати дитині казку, а собі — відпочинок без зайвих турбот.\n"
        "Можу поставити кілька уточнюючих питань, щоб ми підібрали найкращий варіант?"
    )
    await update.message.reply_text(text)
    return STATE_INTRO

async def intro_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()
    # GPT
    adv = await invoke_gpt_experts("intro", user_text, context.user_data)
    logger.info(f"GPT Experts [INTRO]:\n{adv}")

    if any(x in user_text for x in ["так", "да", "ок", "добре", "хочу"]):
        await update.message.reply_text(
            "Супер! Скажіть, будь ласка, з якого міста ви б хотіли виїжджати (Ужгород чи Мукачево) "
            "і скільки у вас дітей?"
        )
        return STATE_NEEDS
    else:
        await update.message.reply_text(
            "Гаразд. Якщо вирішите дізнатись більше — просто напишіть /start або 'Хочу дізнатися'. "
            "Гарного дня!"
        )
        return ConversationHandler.END

async def needs_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    context.user_data["needs_info"] = user_text

    # GPT
    adv = await invoke_gpt_experts("needs", user_text, context.user_data)
    logger.info(f"GPT Experts [NEEDS]:\n{adv}")

    await update.message.reply_text(
        "Зрозуміла вас. Ви не уявляєте, скільки мам вже змогли перезавантажитись і відпочити "
        "завдяки цій поїздці!\n"
        "Дозвольте розповісти трошки про враження, які чекають саме на вас."
    )
    return STATE_PSYCHO

async def psycho_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text

    # GPT
    adv = await invoke_gpt_experts("psycho", user_text, context.user_data)
    logger.info(f"GPT Experts [PSYCHO]:\n{adv}")

    # Усилим FOMO + соцдоказ
    await update.message.reply_text(
        "Наш тур вже обрали понад 200 сімей за останні місяці. Уявіть радість дитини, "
        "коли вона вперше бачить морських котиків, левів та жирафів буквально у кількох кроках! "
        "А ви в цей час можете просто насолодитися моментом — усе організовано.\n\n"
        "За вашим бажанням розкажу детальніше про програму та умови. "
        "Хочете почути повну презентацію нашого туру?"
    )
    return STATE_PRESENTATION

async def presentation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("presentation", user_text, context.user_data)
    logger.info(f"GPT Experts [PRESENTATION]:\n{adv}")

    # Если «да», «так», «хочу» и т.п.
    if any(x in user_text for x in ["так", "да", "хочу", "детальніше"]):
        # Усиленная презентация (якорение + психология)
        await update.message.reply_text(
            "🔸 *Програма туру*:\n"
            "  • Виїзд о 2:00 з Ужгорода (або Мукачева) на комфортному автобусі — м'які сидіння, "
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
        return STATE_OBJECTIONS
    else:
        await update.message.reply_text("Гаразд, якщо зміните думку — я поруч. Гарного дня!")
        return ConversationHandler.END

async def objections_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("objections", user_text, context.user_data)
    logger.info(f"GPT Experts [OBJECTIONS]:\n{adv}")

    if "дорого" in user_text or "ціна" in user_text:
        await update.message.reply_text(
            "Розумію ваші хвилювання щодо бюджету. Проте зважте, що в 1900 грн "
            "вже включені всі квитки, страховка, супровід. "
            "І ви економите купу часу — не треба шукати, де купити квитки чи як дістатися.\n"
            "А враження дитини — це безцінно. Як вам такий підхід?"
        )
        return STATE_OBJECTIONS
    elif "безпека" in user_text or "дитина боїться" in user_text or "переживаю" in user_text:
        await update.message.reply_text(
            "Ми якраз орієнтуємось на сім'ї з дітьми від 4 років. "
            "У зоопарку є безпечні зони для малечі, а наш супроводжуючий завжди поруч, "
            "щоб допомогти і підтримати.\n"
            "У більшості дітей виявляється навіть більший інтерес, ніж страх!"
        )
        return STATE_OBJECTIONS
    elif any(x in user_text for x in ["ок", "зрозуміло", "гаразд", "не маю"]):
        await update.message.reply_text(
            "Супер! Тоді давайте ще раз уточнимо фінальні цифри та умови оплати. Гаразд?"
        )
        return STATE_QUOTE
    else:
        await update.message.reply_text(
            "Можливо, є ще якісь сумніви? Спробуйте сформулювати їх."
        )
        return STATE_OBJECTIONS

async def quote_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("quote", user_text, context.user_data)
    logger.info(f"GPT Experts [QUOTE]:\n{adv}")

    await update.message.reply_text(
        "Отже, підсумуємо:\n"
        "• Вартість: 1900 грн (дорослий), 1850 грн (дитина).\n"
        "• Це вже включає всі витрати (трансфер, вхідні квитки, страхування, супровід).\n"
        "• Для дітей до 6 років передбачені знижки.\n"
        "• Оплата: 30% передоплата для бронювання місця, решта — за 3 дні до поїздки.\n\n"
        "Чи є ще якісь питання щодо туру або оплати?"
    )
    return STATE_FAQ

async def faq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("faq", user_text, context.user_data)
    logger.info(f"GPT Experts [FAQ]:\n{adv}")

    if "так" in user_text or "є питання" in user_text:
        await update.message.reply_text(
            "Звісно, я тут, щоб відповісти на всі ваші питання. Що саме вас цікавить?"
        )
        return STATE_FAQ
    else:
        await update.message.reply_text(
            "Чудово! Тоді давайте перевіримо, чи готові ви до бронювання місця. "
            "Хочете забронювати місце на найближчу дату?"
        )
        return STATE_FEEDBACK

async def feedback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("feedback", user_text, context.user_data)
    logger.info(f"GPT Experts [FEEDBACK]:\n{adv}")

    if any(x in user_text for x in ["так", "хочу", "бронюю"]):
        await update.message.reply_text(
            "Чудово! Для бронювання потрібно внести передоплату 30%. "
            "Ви готові зробити це зараз?"
        )
        return STATE_PAYMENT
    elif any(x in user_text for x in ["ні", "не зараз", "подумаю"]):
        await update.message.reply_text(
            "Розумію. Можливо, ви хочете зарезервувати місце без оплати? "
            "Ми можемо тримати його для вас 24 години."
        )
        return STATE_RESERVATION
    else:
        await update.message.reply_text(
            "Вибачте, я не зовсім зрозуміла вашу відповідь. "
            "Ви хочете забронювати місце зараз чи, можливо, потрібно більше часу на роздуми?"
        )
        return STATE_FEEDBACK

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("payment", user_text, context.user_data)
    logger.info(f"GPT Experts [PAYMENT]:\n{adv}")

    if any(x in user_text for x in ["так", "готовий", "як оплатити"]):
        await update.message.reply_text(
            "Чудово! Ось наші реквізити для оплати:\n"
            "[Тут будуть реквізити]\n\n"
            "Після оплати, будь ласка, надішліть скріншот чеку. "
            "Як тільки ми отримаємо підтвердження, я передам вас живому менеджеру "
            "для завершення бронювання. Дякую за довіру!"
        )
        return STATE_TRANSFER
    else:
        await update.message.reply_text(
            "Зрозуміло. Якщо вам потрібен час на роздуми, ми можемо зарезервувати місце на 24 години без оплати. "
            "Хочете скористатися цією можливістю?"
        )
        return STATE_RESERVATION

async def reservation_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("reservation", user_text, context.user_data)
    logger.info(f"GPT Experts [RESERVATION]:\n{adv}")

    if any(x in user_text for x in ["так", "хочу", "резервую"]):
        await update.message.reply_text(
            "Чудово! Я зарезервувала для вас місце на 24 години. "
            "Протягом цього часу ви можете повернутися та завершити бронювання. "
            "Якщо у вас виникнуть додаткові питання, не соромтеся звертатися. "
            "Дякую за інтерес до нашого туру!"
        )
        return STATE_FINISH
    else:
        await update.message.reply_text(
            "Зрозуміло. Якщо ви передумаєте або у вас виникнуть додаткові питання, "
            "будь ласка, не соромтеся звертатися. Ми завжди раді допомогти!"
        )
        return STATE_FINISH

async def transfer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("transfer", user_text, context.user_data)
    logger.info(f"GPT Experts [TRANSFER]:\n{adv}")

    await update.message.reply_text(
        "Дякую за вашу оплату! Я передаю вас нашому живому менеджеру для завершення бронювання. "
        "Він зв'яжеться з вами найближчим часом для уточнення деталей. "
        "Якщо у вас виникнуть додаткові питання до того часу, не соромтеся звертатися до мене. "
        "Дякую за вибір нашого туру!"
    )
    return STATE_FINISH

async def finish_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.lower()

    # GPT
    adv = await invoke_gpt_experts("finish", user_text, context.user_data)
    logger.info(f"GPT Experts [FINISH]:\n{adv}")

    await update.message.reply_text(
        "Дякую за спілкування! Якщо у вас виникнуть додаткові питання або ви захочете повернутися "
        "до бронювання, просто напишіть мені. Бажаю гарного дня!"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels and ends the conversation."""
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name)
    await update.message.reply_text(
        'Дякую за спілкування! Якщо захочете повернутися до бронювання, просто напишіть /start.'
    )
    return ConversationHandler.END

def main():
    if is_bot_already_running():
        logger.error("Another instance of the bot is already running. Exiting.")
        sys.exit(1)

    # Указываем временную зону
    tz = timezone(timedelta(hours=2))  # UTC+2 for Kiev

    # Логируем используемую временную зону
    logger.info(f"Используемая временная зона: {tz}")

    # Создаём Application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Устанавливаем часовой пояс в bot_data
    application.bot_data["timezone"] = tz

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            STATE_INTRO: [MessageHandler(filters.TEXT & ~filters.COMMAND, intro_handler)],
            STATE_NEEDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, needs_handler)],
            STATE_PSYCHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, psycho_handler)],
            STATE_PRESENTATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, presentation_handler)],
            STATE_OBJECTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, objections_handler)],
            STATE_QUOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, quote_handler)],
            STATE_FAQ: [MessageHandler(filters.TEXT & ~filters.COMMAND, faq_handler)],
            STATE_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_handler)],
            STATE_PAYMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, payment_handler)],
            STATE_RESERVATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, reservation_handler)],
            STATE_TRANSFER: [MessageHandler(filters.TEXT & ~filters.COMMAND, transfer_handler)],
            STATE_FINISH: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_handler)],
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    application.add_handler(conv_handler)

    # Запускаем бота
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()

