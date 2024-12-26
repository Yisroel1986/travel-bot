import os
import threading
import logging
import openai

from flask import Flask, request, jsonify
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    ConversationHandler,
    CallbackContext
)

# Включаем логирование (по желанию, но полезно)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения из .env
load_dotenv()

# Считываем токен из .env
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# Назначаем ключ OpenAI
openai.api_key = OPENAI_API_KEY

# Для отладки (убедиться, что реально что-то считалось):
print("DEBUG BOT_TOKEN =", BOT_TOKEN)

# Состояния
(
    STATE_INTRO,         # 1. Приветствие / знакомство
    STATE_NEEDS,         # 2. Выявление потребностей
    STATE_PSYCHO,        # 3. Психологические триггеры
    STATE_PRESENTATION,  # 4. Расширенная презентация (продажа)
    STATE_OBJECTIONS,    # 5. Обработка возражений
    STATE_QUOTE,         # 6. Итоговая цена / якорение
    STATE_FAQ,           # 7. Доп. вопросы (FAQ)
    STATE_FEEDBACK,      # 8. Проверяем готовность к покупке
    STATE_PAYMENT,       # 9. Оплата (аванс 30%)
    STATE_RESERVATION,   # 10. Резерв без оплаты
    STATE_TRANSFER,      # 11. Передача менеджеру
    STATE_FINISH         # 12. Завершение диалога
) = range(12)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def start(update, context):
    update.message.reply_text("Привет! Я ваш бот...")

def help_command(update, context):
    update.message.reply_text("Моя помощь...")

def ask_gpt(update, context):
    user_text = update.message.text
    # ... GPT логика ...
    update.message.reply_text("GPT ответ...")

################################
# 1. Функция обращения к ChatGPT (вирт. эксперты)
################################

def invoke_gpt_experts(stage: str, user_text: str, context_data: dict):
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
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Ты – умный помощник, отвечай лаконично, но точно."},
                {"role": "user", "content": user_text},
            ],
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

################################
# 2. Handlers для сцен
################################

def start_command(update: Update, context: CallbackContext):
    user_name = mention_user(update)
    # Советы от экспертов
    adv = invoke_gpt_experts("intro", "/start", context.user_data)
    logger.info(f"GPT Experts [INTRO]:\n{adv}")

    text = (
        f"Привіт, {user_name}! Я Марія, ваш віртуальний тур-менеджер. "
        "Дякую, що зацікавились нашою сімейною поїздкою до зоопарку Ньїредьгаза.\n\n"
        "Це ідеальний спосіб подарувати дитині казку, а собі — відпочинок без зайвих турбот.\n"
        "Можу поставити кілька уточнюючих питань, щоб ми підібрали найкращий варіант?"
    )
    update.message.reply_text(text)
    return STATE_INTRO

def intro_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()
    # GPT
    adv = invoke_gpt_experts("intro", user_text, context.user_data)
    logger.info(f"GPT Experts [INTRO]:\n{adv}")

    if any(x in user_text for x in ["так", "да", "ок", "добре", "хочу"]):
        update.message.reply_text(
            "Супер! Скажіть, будь ласка, з якого міста ви б хотіли виїжджати (Ужгород чи Мукачево) "
            "і скільки у вас дітей?"
        )
        return STATE_NEEDS
    else:
        update.message.reply_text(
            "Гаразд. Якщо вирішите дізнатись більше — просто напишіть /start або 'Хочу дізнатися'. "
            "Гарного дня!"
        )
        return ConversationHandler.END

def needs_handler(update: Update, context: CallbackContext):
    user_text = update.message.text
    context.user_data["needs_info"] = user_text

    # GPT
    adv = invoke_gpt_experts("needs", user_text, context.user_data)
    logger.info(f"GPT Experts [NEEDS]:\n{adv}")

    update.message.reply_text(
        "Зрозуміла вас. Ви не уявляєте, скільки мам вже змогли перезавантажитись і відпочити "
        "завдяки цій поїздці!\n"
        "Дозвольте розповісти трошки про враження, які чекають саме на вас."
    )
    return STATE_PSYCHO

def psycho_handler(update: Update, context: CallbackContext):
    user_text = update.message.text

    # GPT
    adv = invoke_gpt_experts("psycho", user_text, context.user_data)
    logger.info(f"GPT Experts [PSYCHO]:\n{adv}")

    # Усилим FOMO + соцдоказ
    update.message.reply_text(
        "Наш тур вже обрали понад 200 сімей за останні місяці. Уявіть радість дитини, "
        "коли вона вперше бачить морських котиків, левів та жирафів буквально у кількох кроках! "
        "А ви в цей час можете просто насолодитися моментом — усе організовано.\n\n"
        "За вашим бажанням розкажу детальніше про програму та умови. "
        "Хочете почути повну презентацію нашого туру?"
    )
    return STATE_PRESENTATION

def presentation_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("presentation", user_text, context.user_data)
    logger.info(f"GPT Experts [PRESENTATION]:\n{adv}")

    # Если «да», «так», «хочу» и т.п.
    if any(x in user_text for x in ["так", "да", "хочу", "детальніше"]):
        # Усиленная презентация (якорение + психология)
        update.message.reply_text(
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
        update.message.reply_text("Гаразд, якщо зміните думку — я поруч. Гарного дня!")
        return ConversationHandler.END

def objections_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("objections", user_text, context.user_data)
    logger.info(f"GPT Experts [OBJECTIONS]:\n{adv}")

    if "дорого" in user_text or "ціна" in user_text:
        update.message.reply_text(
            "Розумію ваші хвилювання щодо бюджету. Проте зважте, що в 1900 грн "
            "вже включені всі квитки, страховка, супровід. "
            "І ви економите купу часу — не треба шукати, де купити квитки чи як дістатися.\n"
            "А враження дитини — це безцінно. Як вам такий підхід?"
        )
        return STATE_OBJECTIONS
    elif "безпека" in user_text or "дитина боїться" in user_text or "переживаю" in user_text:
        update.message.reply_text(
            "Ми якраз орієнтуємось на сім'ї з дітьми від 4 років. "
            "У зоопарку є безпечні зони для малечі, а наш супроводжуючий завжди поруч, "
            "щоб допомогти і підтримати.\n"
            "У більшості дітей виявляється навіть більший інтерес, ніж страх!"
        )
        return STATE_OBJECTIONS
    elif any(x in user_text for x in ["ок", "зрозуміло", "гаразд", "не маю"]):
        update.message.reply_text(
            "Супер! Тоді давайте ще раз уточнимо фінальні цифри та умови оплати. Гаразд?"
        )
        return STATE_QUOTE
    else:
        update.message.reply_text(
            "Можливо, є ще якісь сумніви? Спробуйте сформулювати їх."
        )
        return STATE_OBJECTIONS

def quote_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("quote", user_text, context.user_data)
    logger.info(f"GPT Experts [QUOTE]:\n{adv}")

    update.message.reply_text(
        "Отже, підсумуємо:\n"
        "• Вартість: 1900 грн (дорослий), 1850 грн (дитина).\n"
        "• Це вже включає всі витрати (трансфер, вхідні квитки, страхування, супровід).\n"
        "• Для дітей до 6 років передбачені знижки.\n\n"
        "Якщо є додаткові питання — пишіть. "
        "Якщо все зрозуміло, можемо перейти до бронювання (30% аванс)."
    )
    return STATE_FAQ

def faq_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("faq", user_text, context.user_data)
    logger.info(f"GPT Experts [FAQ]:\n{adv}")

    if any(x in user_text for x in ["так", "faq", "питання"]):
        update.message.reply_text(
            "Типові запитання:\n\n"
            "1. *Обід* — за власний рахунок, можна брати бутерброди.\n"
            "2. *Документи* — достатньо біометричного паспорта.\n"
            "3. *Діти* — від 4 років, із супроводом дорослих.\n"
            "4. *Оплата* — 30% передоплати на карту Приват/Моно, решта перед виїздом.\n\n"
            "Якщо все ок — напишіть 'Готові бронювати!' або 'Продовжимо'."
        )
        return STATE_FAQ
    elif any(x in user_text for x in ["готові", "продовжимо", "ок"]):
        update.message.reply_text(
            "Чудово! Зручно оплатити Приват чи Моно?"
        )
        return STATE_FEEDBACK
    else:
        update.message.reply_text("Не зовсім зрозуміла. Можливо, ви готові бронювати?")
        return STATE_FAQ

def feedback_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("feedback", user_text, context.user_data)
    logger.info(f"GPT Experts [FEEDBACK]:\n{adv}")

    if "приват" in user_text or "mono" in user_text or "моно" in user_text:
        update.message.reply_text(
            "Окей! Ось реквізити:\n"
            "Приват: 4141 XXXX XXXX 1111\n"
            "Моно: 5375 XXXX XXXX 2222\n\n"
            "Внесіть, будь ласка, 30% від загальної суми. "
            "Після оплати напишіть 'Оплатила', і я зафіксую бронь."
        )
        return STATE_PAYMENT
    elif "думаю" in user_text or "резерв" in user_text or "не впевнена" in user_text:
        update.message.reply_text(
            "Можу запропонувати бронювання без передоплати на 24 години, "
            "щоб ви не втратили місця. Готові скористатися?"
        )
        return STATE_RESERVATION
    elif "менеджер" in user_text or "дзвінок" in user_text:
        update.message.reply_text(
            "Добре, залиште ваш номер телефону, і менеджер зателефонує або напише в месенджер."
        )
        return STATE_TRANSFER
    else:
        update.message.reply_text(
            "Якщо ви поки не готові, не біда. Напишіть будь-коли, якщо появляться питання."
        )
        return STATE_FINISH

def payment_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("payment", user_text, context.user_data)
    logger.info(f"GPT Experts [PAYMENT]:\n{adv}")

    if any(x in user_text for x in ["оплатила", "оплатив", "готово"]):
        update.message.reply_text(
            "Прекрасно! Оплату отримала. Ваша бронь підтверджена. "
            "Я надішлю нагадування за тиждень до виїзду, за 2 дні і за добу.\n"
            "Дякую, що обрали нас! Якщо будуть питання — я на зв'язку."
        )
        return STATE_FINISH
    else:
        update.message.reply_text(
            "Добре, як будете готові, напишіть 'Оплатила'."
        )
        return STATE_PAYMENT

def reservation_handler(update: Update, context: CallbackContext):
    user_text = update.message.text.lower()

    # GPT
    adv = invoke_gpt_experts("reservation", user_text, context.user_data)
    logger.info(f"GPT Experts [RESERVATION]:\n{adv}")

    if any(x in user_text for x in ["так", "ок", "добре"]):
        update.message.reply_text(
            "Чудово! Тоді я бронюю за вами місце на 24 години без передоплати.\n"
            "Якщо не підтвердите оплату за цей час, бронь автоматично знімається.\n"
            "Буду рада вашому поверненню!"
        )
        # Тут можно настроить JobQueue, которая через 24 часа напомнит
        return STATE_FINISH
    else:
        update.message.reply_text(
            "Добре, тоді звертайтеся, якщо надумаєте."
        )
        return STATE_FINISH

def transfer_handler(update: Update, context: CallbackContext):
    user_text = update.message.text
    # GPT
    adv = invoke_gpt_experts("transfer", user_text, context.user_data)
    logger.info(f"GPT Experts [TRANSFER]:\n{adv}")

    context.user_data["manager_info"] = user_text
    update.message.reply_text(
        "Дякую! Передам ваш контакт нашому менеджеру, і він зателефонує.\n"
        "Гарного дня!"
    )
    return STATE_FINISH

def finish_handler(update: Update, context: CallbackContext):
    user_text = update.message.text
    # GPT
    adv = invoke_gpt_experts("finish", user_text, context.user_data)
    logger.info(f"GPT Experts [FINISH]:\n{adv}")

    update.message.reply_text(
        "Дякую за ваш час і цікавість! "
        "Якщо ще будуть запитання або захочете оформити тур, просто напишіть /start.\n"
        "Гарного дня!",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END


def fallback_handler(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Вибачте, не зовсім зрозуміла. Напишіть /start або 'Привіт'."
    )
    return ConversationHandler.END

app = Flask(__name__)

@app.route('/')
def index():
    """Простая проверка, что сервер работает."""
    return "Hello, I'm a Telegram polling bot + Flask Web Service for future FB/IG"

def run_telegram_polling():
    """Запускаем polling для Телеграма в отдельном потоке."""
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан! Проверь переменные окружения.")
        return

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Регистрируем команды и обработчики
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, ask_gpt))

    # Запуск polling
    updater.start_polling()
    updater.idle()

def main():
    """Основная точка входа в программу."""
    # Проверяем, что токен не пуст
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN is not set!")
        # Вместо return (выйти из функции) можно завершить программу
        import sys
        sys.exit(1)

    if not OPENAI_API_KEY:
        print("Warning: OPENAI_API_KEY is not set!")
        # Здесь можно не выходить полностью, но GPT-советы работать не будут
        # return

    # Создаём Updater и берём токен
    updater = Updater(BOT_TOKEN, use_context=True)

    # Получаем диспетчер (dispatcher) для регистрации хендлеров
    dp = updater.dispatcher

    # =========================================
    # Регистрируем обработчики команд/сообщений
    # (Пример: /start, /help, и MessageHandler, который вызывает ask_gpt)
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, ask_gpt))
    # =========================================

    # Функция, которая запускает телеграм-бот в режиме polling.
    def run_telegram_polling():
        """Запускаем polling для Телеграма в отдельном потоке."""
        logger.info("Starting Telegram polling...")
        updater.start_polling()
        updater.idle()

    # ============== Запуск в отдельном потоке ==============
    polling_thread = threading.Thread(target=run_telegram_polling, daemon=True)
    polling_thread.start()

    # ============== Запуск Flask-сервера ===================
    # Предположим, что выше в коде у нас объявлено:
    # app = Flask(__name__)

    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Starting Flask on port {port}...")
    app.run(host='0.0.0.0', port=port)

if __name__ == "__main__":
    main()
