# gpt_logic.py

import os
import openai
from scenario import ScenarioData

# Убедитесь, что у вас выставлен OPENAI_API_KEY в переменных окружения
openai.api_key = os.getenv("OPENAI_API_KEY")

class GPTLogic:
    """
    Класс, отвечающий за логику определения ответа:
    1) Пытается сопоставить со сценариями (Laplandia / Zoo / FAQ).
    2) Если нет точных совпадений => GPT fallback.
    3) Использует Tone of Voice (женский образ, эмпатия).
    """

    def __init__(self):
        # Можем заранее загрузить сценарии
        self.laplandia_scenario = ScenarioData.get_scenario_for_laplandia()
        self.zoo_scenario = ScenarioData.get_scenario_for_nyiregyhaza()
        self.faq_data = ScenarioData.get_faq_common()

    def get_response(self, user_text: str) -> str:
        """
        Основной метод получения ответа:
         - проверяем, не asked ли пользователь про лагерь/зоопарк 
         - проверяем FAQ
         - если ничего не подошло, GPT fallback
        """

        # 1) Небольшой шажок: приводим к нижнему регистру
        text_lower = user_text.lower()

        # 2) Проверка на ключевые слова для "Лапландія"
        if any(k in text_lower for k in ["лапланд", "карпат", "лагерь"]):
            # Выдаём intro или другие сегменты
            return self.laplandia_scenario["intro"]

        # 3) Проверка на ключевые слова для "зоопарк" (Ньїредьгаза)
        if any(k in text_lower for k in ["зоопарк", "ніредьгаза", "nyire", "лев"]):
            return self.zoo_scenario["intro"]

        # 4) Проверка FAQ
        # Например, пользователь спрашивает "Какая цена?" или "Сколько стоит?" => "цена"
        if "цена" in text_lower or "скільки коштує" in text_lower:
            return self.faq_data.get("цена", "Ценовая информация сейчас недоступна.")
        if "безопасн" in text_lower or "безпека" in text_lower:
            return self.faq_data.get("безопасность", "Мы всегда уделяем внимание безопасности!")
        if "оплата частями" in text_lower or "рассрочка" in text_lower:
            return self.faq_data.get("оплата частями", "Можем обсудить гибкие условия оплаты.")
        if "возраст" in text_lower or "лет ребенку" in text_lower:
            return self.faq_data.get("возраст детей", "Уточните возраст, и я расскажу, что подходит!")

        # 5) Если ничего не подошло => GPT fallback
        fallback_answer = self.call_gpt_fallback(user_text)
        return fallback_answer

    def call_gpt_fallback(self, user_text: str) -> str:
        """
        Обращение к GPT (пример с gpt-4). 
        Добавляем системный prompt, где прописываем 
        женский образ, эмпатию, стиль общения и т.д.
        """
        system_prompt = (
            "Ты — чат-бот-женщина по имени Олена, дружелюбная, эмпатичная, работаешь на украинском/русском языках "
            "для продажи туров и детских лагерей. "
            "У тебя есть большой бриф со сценарием, но если не нашла точного ответа — "
            "отвечай теплом, вежливо и профессионально. "
            "Всегда старайся использовать эмодзи уместно, обращаться к клиенту с пониманием. "
        )

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ]

            response = openai.ChatCompletion.create(
                model="gpt-4",   # Или gpt-3.5-turbo, смотря что доступно
                messages=messages,
                max_tokens=300,
                temperature=0.7
            )
            gpt_text = response.choices[0].message.content.strip()
            return gpt_text

        except Exception as e:
            print("GPT Fallback error:", e)
            # Если GPT не сработало, вернём стандартный fallback
            return ScenarioData.get_fallback_text()
