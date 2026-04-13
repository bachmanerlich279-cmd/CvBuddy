import os
import json
import asyncio
import logging
from aiohttp import web
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from openai import AsyncOpenAI
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML

# Жорстка перевірка наявності ключів з Environment Variables
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    raise ValueError("Критична помилка: Ключі не знайдені в environment variables.")

# Ініціалізація компонентів Телеграм-бота та OpenAI
logging.basicConfig(level=logging.INFO)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Налаштування Jinja2 для HTML-шаблонів (шукає файли в поточній папці)
env = Environment(loader=FileSystemLoader('.'))

# Тимчасова пам'ять діалогів
user_conversations = {}

SYSTEM_PROMPT = """
Ти — нестандартний, брутальний, але суперефективний ШІ-рекрутер. Твій стиль: прямий, зухвалий, похмурий і різкий, неначе ми в чорно-білому кліпі під трек $uicideboy$.
Ти орієнтований на результат. Ніякої корпоративної води.
Якщо юзер проявляє агресію — дзеркаль її жорстко, але одразу переводь у конструктив (записуй як стресостійкість).
Мета: за 20 хвилин витягнути з користувача його досвід, навички та освіту.
Коли збереш достатньо даних, скажи юзеру натиснути команду /generate.
"""

JSON_EXTRACTOR_PROMPT = """
Ти — системний екстрактор. Проаналізуй історію діалогу і витягни дані у СТРОГОМУ форматі JSON. 
Якщо якихось даних бракує — придумай правдоподібні заглушки на основі контексту.
Очікувана JSON структура:
{
  "full_name": "Ім'я Прізвище",
  "profession": "Бажана посада",
  "phone": "+380...",
  "email": "email@example.com",
  "profile": "Професійне самарі у впевненому стилі",
  "experience": [
    {"title": "Посада", "company": "Компанія", "years": "Роки", "description": "Опис"}
  ],
  "skills": ["Навичка 1", "Навичка 2"],
  "education": [
    {"degree": "Ступінь", "institution": "Заклад", "years": "Роки"}
  ]
}
Поверни ТІЛЬКИ валідний JSON.
"""

@router.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    user_conversations[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    greeting = (
        "Йоу. Я тут не для того, щоб витирати тобі соплі. "
        "Я тут, щоб зробити з твого життєвого досвіду шикарне резюме. "
        "Розказуй, чим ти займаєшся, і давай по суті."
    )
    user_conversations[user_id].append({"role": "assistant", "content": greeting})
    await message.answer(greeting)

@router.message(Command("generate"))
async def cmd_generate(message: Message):
    user_id = message.from_user.id
    if user_id not in user_conversations or len(user_conversations[user_id]) < 3:
        await message.answer("Ми ще навіть не почали нормально говорити. Розкажи про свій досвід.")
        return

    # Клавіатура з вибором вайбу
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖤 Темний Градієнт", callback_data="tpl_dark")],
        [InlineKeyboardButton(text="🔵 Синьо-сірий (Corporate)", callback_data="tpl_blue")],
        [InlineKeyboardButton(text="⚪ Сіро-білий (Minimalist)", callback_data="tpl_minimal")]
    ])
    
    await message.answer("Інформацію зібрано. Обери дизайн для свого резюме, бро:", reply_markup=keyboard)

@router.callback_query(lambda c: c.data and c.data.startswith('tpl_'))
async def process_template_selection(callback_query: CallbackQuery):
    user_id = callback_query.from_user.id
    template_choice = callback_query.data
    
    # Прибираємо кнопки після натискання
    await bot.edit_message_reply_markup(chat_id=user_id, message_id=callback_query.message.message_id, reply_markup=None)
    await bot.send_message(user_id, "Прийнято. Пакуємо твоє життя у JSON, а потім — у вибраний PDF. Зачекай...")

    # Маршрутизація шаблонів
    templates_map = {
        "tpl_dark": "template_dark.html",
        "tpl_blue": "template_blue.html",
        "tpl_minimal": "template_minimal.html"
    }
    
    selected_html_file = templates_map.get(template_choice, "template_dark.html")
    
    try:
        selected_template = env.get_template(selected_html_file)
    except Exception as e:
        await bot.send_message(user_id, f"Шаблон {selected_html_file} ще не знайдено. Використовую бекап (template_dark.html).")
        selected_template = env.get_template("template_dark.html")

    history_text = "\n".join([f"{msg['role']}: {msg['content']}" for msg in user_conversations.get(user_id, [])])
    messages_for_extraction = [
        {"role": "system", "content": JSON_EXTRACTOR_PROMPT},
        {"role": "user", "content": f"Ось історія бесіди:\n{history_text}"}
    ]

    try:
        # Екстракція JSON (gpt-4o)
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages_for_extraction,
            temperature=0.2,
            response_format={ "type": "json_object" }
        )
        
        json_data = response.choices[0].message.content
        resume_data = json.loads(json_data)
        
        # Рендер HTML -> PDF
        html_out = selected_template.render(resume_data)
        pdf_filename = f"resume_{user_id}.pdf"
        HTML(string=html_out).write_pdf(pdf_filename)
        
        # Відправка файлу
        doc = FSInputFile(pdf_filename)
        await bot.send_document(chat_id=user_id, document=doc, caption="Твоє резюме готове. Розривай ринок.")
        
        # Зачистка
        os.remove(pdf_filename)
        user_conversations[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    except Exception as e:
        logging.error(f"Помилка при генерації PDF або JSON: {e}")
        await bot.send_message(user_id, "Система дала збій на етапі рендеру. Перевір логи.")

@router.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
    
    if user_id not in user_conversations:
        user_conversations[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        
    user_conversations[user_id].append({"role": "user", "content": message.text})
    
    try:
        # Швидка бесіда (gpt-4o-mini)
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=user_conversations[user_id],
            temperature=0.8 
        )
        reply = response.choices[0].message.content
        user_conversations[user_id].append({"role": "assistant", "content": reply})
        
        await message.answer(reply)
    except Exception as e:
        logging.error(f"Помилка в чаті: {e}")
        await message.answer("Зв'язок обірвався. Повтори ще раз.")

# ==========================================
# БЛОК ДЛЯ ХОСТИНГУ НА RENDER
# ==========================================

async def health_check(request):
    """Фейкова відповідь веб-сервера, щоб Render не вимкнув додаток."""
    return web.Response(text="Бот живий і готовий до роботи.")

async def main():
    print("ШІ-рекрутер запущений. Сигнатуру підтверджено.")
    
    # Запускаємо Телеграм-бота у фоновому завданні
    bot_task = asyncio.create_task(dp.start_polling(bot))
    
    # Піднімаємо обманку веб-сервера для Render
    app = web.Application()
    app.router.add_get('/', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render автоматично видає порт через змінну середовища PORT
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    
    print(f"Веб-сервер запущено на порту {port}")
    
    # Тримаємо процес активним
    await bot_task

if __name__ == "__main__":
    asyncio.run(main())
