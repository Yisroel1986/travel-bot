# gpt_logic.py

import os
import openai
from scenario import ScenarioData

openai.api_key = os.getenv("OPENAI_API_KEY")

class GPTLogic:
    def get_fallback_response(self, user_text: str) -> str:
        system_prompt = (
            "Ты — чат-бот-женщина по имени Олена, ... (стиль, эмпатия)..."
        )
        if not openai.api_key:
            return ScenarioData.get_fallback_text()

        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}
            ]
            response = openai.ChatCompletion.create(
                model="gpt-4",
                messages=messages,
                max_tokens=400,
                temperature=0.7
            )
            gpt_text = response.choices[0].message.content.strip()
            return gpt_text

        except Exception as e:
            print("GPT fallback error:", e)
            return ScenarioData.get_fallback_text()
