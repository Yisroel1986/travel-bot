#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Полный код Telegram-бота без JobQueue.
Все вызовы schedule_no_response_job / cancel_no_response_job удалены.

Функции и этапы:
1) SQLite для состояния
2) GPT-4 (условно 'gpt-4-turbo')
3) CRM
4) sentiment-анализ (HuggingFace, VADER)
5) Flask webhook
6) Python Telegram Bot (в режиме webhook, без job_queue)
7) Этапы продаж: STAGE_GREET -> ... -> STAGE_END

Содержит ~700+ строк благодаря подробным комментариям.
"""

import os
import logging
import sys
import psutil
import sqlite3
import json
import asyncio
import threading
import re
import requests
from datetime import datetime
from typing import Optional, Dict, Any

# Flask
from flask import Flask, request

# Telegram Bot
from telegram import (
    Update,
    ReplyKeyboardRemove,
    InputMediaPhoto,
    InputMediaVideo,
)
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
# request для Telegram
from telegram.request import HTTPXRequest

# dotenv для чтения .env
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CRM_API_KEY = os.getenv("CRM_API_KEY")
CRM_API_URL = os.getenv("CRM_API_URL", "https://familyplace.keycrm.app/api/v1/products")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "https://your-app.onrender.com")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Проверка openai
try:
    import openai
    if OPENAI_API_KEY:
        openai.api_key = OPENAI_API_KEY
except:
    openai = None

# spaCy (украинский пайплайн)
try:
    import spacy
    nlp_uk = spacy.load("uk_core_news_sm")
except:
    nlp_uk = None

# transformers/HuggingFace
try:
    from transformers import pipeline
    sentiment_pipeline = pipeline(
        "sentiment-analysis",
        model="nlptown/bert-base-multilingual-uncased-sentiment"
    )
except:
    sentiment_pipeline = None

# VADER
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    vader_analyzer = SentimentIntensityAnalyzer()
except:
    vader_analyzer = None

# deep-translator
try:
    from deep_translator import GoogleTranslator
except:
    GoogleTranslator = None

# Проверка, не запущен ли бот
def is_bot_already_running() -> bool:
    """
    Проверяем, не запущен ли уже бот в другом процессе (например, на Render).
    Если находим процесс с тем же cmdline, завершаем.
    """
    current_process = psutil.Process()
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if (
            process.info['name'] == current_process.name() and
            process.info['cmdline'] == current_process.cmdline() and
            process.info['pid'] != current_process.pid
        ):
            return True
    return False

# Состояния диалога (этапы продаж)
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

# Flask-приложение
app = Flask(__name__)
application: Optional[Application] = None

###########################
# Инициализация БД (SQLite)
###########################

def init_db():
    """
    Создаёт таблицу conversation_state для хранения текущего шага
    и user_data, если не существует.
    """
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
    """
    Возвращает (current_stage, user_data_json) или (None, None)
    """
    conn = sqlite3.connect("bot_database.db")
    c = conn.cursor()
    c.execute("SELECT current_stage, user_data FROM conversation_state WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return row[0], row[1]
    return None, None

def save_user_state(user_id: str, current_stage: int, user_data: dict):
    """
    Сохраняем состояние в SQLite.
    """
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

###########################
# Работа с CRM
###########################

def fetch_all_products():
    """
    Загружаем продукты (туры) из CRM.
    Если CRM_API_KEY или CRM_API_URL отсутствуют, вернём пустой список.
    """
    if not CRM_API_KEY or not CRM_API_URL:
        logger.warning("CRM_API_KEY or CRM_API_URL not found. Returning empty tours list.")
        return []
    headers = {"Authorization": f"Bearer {CRM_API_KEY}", "Accept": "application/json"}
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
                        # current_page = data.get("current_page", page)
                    elif "data" in data and isinstance(data["data"], dict):
                        sub = data["data"]
                        items = sub.get("items", [])
                        all_items.extend(items)
                        total = sub.get("total", len(all_items))
                        # current_page = sub.get("page", page)
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

###########################
# typing_simulation
###########################

async def typing_simulation(update: Update, text: str):
    """
    Эмуляция "набора текста" и отправки сообщения
    """
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(min(4, max(2, len(text)/70)))
    await update.message.reply_text(text, reply_markup=ReplyKeyboardRemove())

###########################
# Анализ intent (spaCy / fallback)
###########################

def is_positive_response(text: str) -> bool:
    arr = ["так","добре","да","ок","продовжуємо","розкажіть","готовий","готова","привіт","hello","расскажи","зацікав","зацікавлений"]
    return any(k in text.lower() for k in arr)

def is_negative_response(text: str) -> bool:
    arr = ["не хочу","не можу","нет","ні","не буду","не зараз"]
    return any(k in text.lower() for k in arr)

def analyze_intent(text: str) -> str:
    """
    Определяем positive / negative / unclear
    """
    if nlp_uk:
        doc = nlp_uk(text)
        lemmas = [token.lemma_.lower() for token in doc]
        pos = {"так","добре","да","ок","продовжувати","розповісти","готовий","готова","привіт","hello","зацікавити","зацікавлений"}
        neg = {"не","нехочу","неможу","нет","ні","небуду","не","не зараз"}
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

###########################
# Анализ тональности (HuggingFace + VADER)
###########################

def get_sentiment(text: str) -> str:
    """
    Возвращает 'positive', 'negative' или 'neutral'.
    """
    if sentiment_pipeline:
        try:
            result = sentiment_pipeline(text)[0]
            label = result.get("label", "")
            parts = label.split()
            if parts:
                stars = int(parts[0])
                if stars <= 2:
                    return "negative"
                elif stars == 3:
                    return "neutral"
                else:
                    return "positive"
        except Exception as e:
            logger.warning(f"HuggingFace sentiment error: {e}")

    if vader_analyzer:
        scores = vader_analyzer.polarity_scores(text)
        compound = scores.get('compound', 0)
        if compound >= 0.05:
            return "positive"
        elif compound <= -0.05:
            return "negative"
        else:
            return "neutral"

    return "negative" if is_negative_response(text) else "neutral"

###########################
# GPT-4.5 (gpt-4-turbo)
###########################

async def get_chatgpt_response(prompt: str) -> str:
    if openai is None or not OPENAI_API_KEY:
        return "Вибачте, функція ChatGPT недоступна."
    try:
        response = await asyncio.to_thread(
            openai.ChatCompletion.create,
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error("Error calling ChatGPT: %s", e)
        return "Вибачте, сталася помилка при генерації відповіді."

###########################
# Этапы продаж (ConversationHandler)
###########################

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - точка входа, проверяем, есть ли незавершённая беседа
    """
    user_id = str(update.effective_user.id)
    init_db()
    # УДАЛЕНО: отмена job queue, schedule (НЕ ИСПОЛЬЗУЕМ)
    stg, dat = load_user_state(user_id)
    if stg is not None and dat is not None:
        text = (
            "Ви маєте незавершену розмову. "
            "Бажаєте продовжити з того ж місця чи почати заново?\n"
            "Відповідайте: 'Продовжити' або 'Почати заново'."
        )
        await typing_simulation(update, text)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET
    else:
        txt = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, txt)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET

async def greet_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обработчик на STAGE_GREET
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.strip()

    if "продовжити" in txt.lower():
        stg, dat = load_user_state(user_id)
        if stg is not None:
            context.user_data.update(json.loads(dat))
            resp = "Повертаємось до попередньої розмови."
            await typing_simulation(update, resp)
            return stg
        else:
            r = "Немає попередніх даних, почнемо з нуля."
            await typing_simulation(update, r)
            save_user_state(user_id, STAGE_GREET, context.user_data)
            return STAGE_GREET

    if "почати" in txt.lower() or "заново" in txt.lower():
        context.user_data.clear()
        g = (
            "Вітаю вас! 😊 Ви зацікавились одноденним туром в зоопарк Ньїредьгаза, Угорщина. "
            "Дозвольте задати кілька уточнюючих питань. Добре?"
        )
        await typing_simulation(update, g)
        save_user_state(user_id, STAGE_GREET, context.user_data)
        return STAGE_GREET

    intent = analyze_intent(txt)
    if intent == "positive":
        t = (
            "Дякую за вашу зацікавленість! 😊\n"
            "Звідки вам зручніше виїжджати: з Ужгорода чи Мукачева? 🚌"
        )
        await typing_simulation(update, t)
        save_user_state(user_id, STAGE_DEPARTURE, context.user_data)
        return STAGE_DEPARTURE
    elif intent == "negative":
        m = (
            "Я можу коротко розповісти про наш тур, якщо зараз вам незручно відповідати на питання."
        )
        await typing_simulation(update, m)
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return STAGE_DETAILS

    # GPT fallback
    fp = (
        "В рамках сценарію тура, клієнт написав: " + txt +
        "\nВідповідай українською мовою, дотримуючись сценарію тура."
    )
    fallback_text = await get_chatgpt_response(fp)
    await typing_simulation(update, fallback_text)
    return STAGE_GREET

async def departure_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_DEPARTURE: куда выезжать
    """
    user_id = str(update.effective_user.id)
    d = update.message.text.strip()
    context.user_data["departure"] = d
    r = "Для кого ви розглядаєте цю поїздку? Чи плануєте їхати разом із дитиною?"
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_TRAVEL_PARTY, context.user_data)
    return STAGE_TRAVEL_PARTY

async def travel_party_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_TRAVEL_PARTY
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()
    if "дит" in txt:
        context.user_data["travel_party"] = "child"
        await typing_simulation(update, "Скільки років вашій дитині?")
        save_user_state(user_id, STAGE_CHILD_AGE, context.user_data)
        return STAGE_CHILD_AGE
    context.user_data["travel_party"] = "no_child"
    r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_CHOICE, context.user_data)
    return STAGE_CHOICE

async def child_age_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_CHILD_AGE
    """
    user_id = str(update.effective_user.id)
    t = update.message.text.strip()
    if t.isdigit():
        context.user_data["child_age"] = t
        r = "Що вас цікавить найбільше: деталі туру, вартість чи бронювання місця? 😊"
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        return STAGE_CHOICE
    if any(x in t.lower() for x in ["детал","вартість","ціна","брон"]):
        context.user_data["child_age"] = "unspecified"
        rr = "Добре, перейдемо далі."
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_CHOICE, context.user_data)
        return STAGE_CHOICE
    await typing_simulation(update, "Будь ласка, вкажіть вік дитини або задайте інше питання.")
    return STAGE_CHILD_AGE

async def choice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_CHOICE: детали, цена, бронь
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    if "деталь" in txt or "деталі" in txt:
        context.user_data["choice"] = "details"
        save_user_state(user_id, STAGE_DETAILS, context.user_data)
        return await details_handler(update, context)
    elif "вартість" in txt or "ціна" in txt:
        context.user_data["choice"] = "cost"
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
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return STAGE_CLOSE_DEAL

    resp = "Будь ласка, уточніть: вас цікавлять деталі туру, вартість чи бронювання місця?"
    await typing_simulation(update, resp)
    save_user_state(user_id, STAGE_CHOICE, context.user_data)
    return STAGE_CHOICE

async def details_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_DETAILS: выдаём детали или стоимость
    """
    user_id = str(update.effective_user.id)
    choice = context.user_data.get("choice","details")
    prods = fetch_all_products()
    txt = update.message.text.lower()

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
            + tours_info
        )
    else:
        text = (
            "Дата виїзду: 26 жовтня з Ужгорода чи Мукачева.\n"
            "Тривалість: Цілий день.\n"
            "Транспорт: Комфортабельний автобус.\n"
            "Зоопарк: Більше 500 видів тварин.\n"
            "Вартість: 1900 грн (трансфер, квитки, страхування).\n\n"
            + tours_info
        )

    await typing_simulation(update, text)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    await update.effective_chat.send_message(text="Чи є у вас додаткові запитання щодо програми туру? 😊")
    return STAGE_ADDITIONAL_QUESTIONS

async def additional_questions_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_ADDITIONAL_QUESTIONS
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    time_keys = ["коли виїзд","коли відправлення","час виїзду","коли автобус","коли вирушаємо"]
    if any(k in txt for k in time_keys):
        ans = (
            "Виїзд о 6:00 з Ужгорода, о 6:30 з Мукачева, повертаємось орієнтовно о 20:00.\n"
            "Чи є ще запитання?"
        )
        await typing_simulation(update, ans)
        save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
        return STAGE_ADDITIONAL_QUESTIONS

    book_keys = ["бронювати","бронюй","купувати тур","давай бронювати","окей давай бронювати","окей бронюй тур"]
    if any(k in txt for k in book_keys):
        r = "Добре, переходимо до оформлення бронювання. Я надам вам реквізити для оплати."
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return await close_deal_handler(update, context)

    no_more = ["немає","все зрозуміло","все ок","досить","спасибі","дякую"]
    if any(k in txt for k in no_more):
        rr = "Як вам наша пропозиція в цілому? 🌟"
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_IMPRESSION, context.user_data)
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

    ans = "Гарне запитання! Якщо є ще щось, що вас цікавить, будь ласка, питайте.\n\nЧи є ще запитання?"
    await typing_simulation(update, ans)
    save_user_state(user_id, STAGE_ADDITIONAL_QUESTIONS, context.user_data)
    return STAGE_ADDITIONAL_QUESTIONS

async def impression_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_IMPRESSION: общее впечатление
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    pos = ["добре","клас","цікаво","відмінно","супер","підходить","так"]
    neg = ["ні","не цікаво","дорого","завелика","надто"]

    if any(k in txt for k in pos):
        r = (
            "Чудово! 🎉 Давайте забронюємо місце для вас і вашої дитини, щоб забезпечити комфортний відпочинок. "
            "Для цього потрібно внести аванс у розмірі 30% та надіслати фото паспорта або іншого документу. "
            "Після цього я надішлю вам усю необхідну інформацію.\n"
            "Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return STAGE_CLOSE_DEAL
    elif any(k in txt for k in neg):
        rr = "Шкода це чути. Якщо у вас залишилися питання або ви захочете розглянути інші варіанти, звертайтеся."
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END
    else:
        resp = "Дякую за думку! Чи готові ви переходити до бронювання?"
        await typing_simulation(update, resp)
        save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
        return STAGE_CLOSE_DEAL

async def close_deal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_CLOSE_DEAL: бронь.
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    pos = ["приват","моно","оплачу","готов","готова","давайте"]
    if any(k in txt for k in pos):
        r = (
            "Чудово! Ось реквізити для оплати:\n"
            "Картка: 0000 0000 0000 0000 (Family Place)\n\n"
            "Після оплати надішліть, будь ласка, скріншот для підтвердження бронювання."
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        return STAGE_PAYMENT

    neg = ["ні","нет","не буду","не хочу"]
    if any(k in txt for k in neg):
        r2 = "Зрозуміло. Буду рада допомогти, якщо передумаєте!"
        await typing_simulation(update, r2)
        save_user_state(user_id, STAGE_END, context.user_data)
        return STAGE_END

    r3 = "Дякую! Ви готові завершити оформлення? Вам зручніше оплатити через ПриватБанк чи MonoBank? 💳"
    await typing_simulation(update, r3)
    save_user_state(user_id, STAGE_CLOSE_DEAL, context.user_data)
    return STAGE_CLOSE_DEAL

async def payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_PAYMENT: ждём оплаты
    """
    user_id = str(update.effective_user.id)
    txt = update.message.text.lower().strip()

    if any(k in txt for k in ["оплатив","відправив","скинув","готово"]):
        r = (
            "Дякую! Тепер перевірю надходження. Як тільки все буде ок, я надішлю деталі поїздки і підтвердження бронювання!"
        )
        await typing_simulation(update, r)
        save_user_state(user_id, STAGE_PAYMENT_CONFIRM, context.user_data)
        return STAGE_PAYMENT_CONFIRM
    else:
        rr = "Якщо виникли додаткові питання — я на зв'язку. Потрібна допомога з оплатою?"
        await typing_simulation(update, rr)
        save_user_state(user_id, STAGE_PAYMENT, context.user_data)
        return STAGE_PAYMENT

async def payment_confirm_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    STAGE_PAYMENT_CONFIRM: завершаем
    """
    user_id = str(update.effective_user.id)
    r = (
        "Дякую за бронювання! Ми успішно зберегли за вами місце. Найближчим часом я надішлю всі деталі. "
        "Якщо є питання — пишіть!"
    )
    await typing_simulation(update, r)
    save_user_state(user_id, STAGE_END, context.user_data)
    return STAGE_END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /cancel - досрочное завершение диалога
    """
    user = update.message.from_user
    logger.info("User %s canceled the conversation.", user.first_name if user else "Unknown")
    t = "Гаразд, завершуємо розмову. Якщо виникнуть питання, завжди можете звернутися знову!"
    await typing_simulation(update, t)
    uid = str(update.effective_user.id)
    save_user_state(uid, STAGE_END, context.user_data)
    return ConversationHandler.END

###########################
# Flask endpoints
###########################

@app.route('/')
def index():
    return "Сервер працює! Бот активний."

@app.route('/webhook', methods=['POST'])
def webhook():
    """
    Приходит update от Telegram. Обрабатываем его, если есть application.
    """
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

###########################
# Запуск бота
###########################

async def run_bot():
    if is_bot_already_running():
        logger.error("Another instance is already running. Exiting.")
        sys.exit(1)

    logger.info("Starting bot...")

    req = HTTPXRequest(connect_timeout=20, read_timeout=40)
    app_builder = ApplicationBuilder().token(BOT_TOKEN).request(req)
    global application
    application = app_builder.build()

    # ConversationHandler со всеми этапами
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
            STAGE_END: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: c.bot.send_message(chat_id=u.effective_chat.id, text="Дякую! Якщо виникнуть питання — /start."))],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)

    # Настройка вебхука
    await setup_webhook(WEBHOOK_URL, application)
    await application.initialize()
    await application.start()

    # Сохраняем event loop
    loop = asyncio.get_running_loop()
    application.bot_data["loop"] = loop

    logger.info("Bot is online and ready.")

async def setup_webhook(url: str, app_ref: Application):
    webhook_url = f"{url}/webhook"
    await app_ref.bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to: {webhook_url}")

def start_flask():
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Flask on port {port}")
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    bot_thread = threading.Thread(target=lambda: asyncio.run(run_bot()), daemon=True)
    bot_thread.start()
    logger.info("Bot thread started. Now starting Flask...")
    start_flask()
