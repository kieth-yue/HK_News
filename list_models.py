import os
from google import genai

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

print("=== 你的 API Key 支援的所有模型 ===")
for m in client.models.list():
    if "flash" in m.name or "pro" in m.name:
        print(m.name)
