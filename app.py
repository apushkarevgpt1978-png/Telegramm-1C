import os, asyncio, aiosqlite, re, uuid
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events, functions, types

app = Quart(__name__)

# --- НАСТРОЙКИ (берутся из твоего YAML) ---
API_ID = int(os.environ.get('API_ID', 23131386))
API_HASH = os.environ.get('API_HASH', '690d8745b7a329c084f2bb092e541002')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
MANAGERS = os.environ.get('MANAGERS_PHONES', '79153019495').split(',')
GROUP_ID = int(os.environ.get('TELEGRAM_GROUP_ID', -1003599844429))
FILES_DIR = '/app/files'
BASE_URL = os.environ.get('BASE_URL', 'http://192.168.121.99:5000')

if not os.path.exists(FILES_DIR): os.makedirs(FILES_DIR)

client = None

async def init_db():
    print(f"!!! DEBUG: Подключение к БД {DB_PATH}")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS outbound_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, phone TEXT, client_name TEXT, client_id TEXT, sender_number TEXT, messenger TEXT DEFAULT 'tg', message_text TEXT, file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER, direction TEXT, error_text TEXT, created_at DATETIME, manager TEXT)")
        await db.execute("CREATE TABLE IF NOT EXISTS topics (topic_id INTEGER PRIMARY KEY, phone TEXT)")
        await db.commit()
    print("!!! DEBUG: БД готова")

async def start_listener():
    global client
    print("!!! DEBUG: Запуск Telethon...")
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: ГЕНА НЕ АВТОРИЗОВАН! Нужно зайти в консоль и ввести код.")
        return
    print("✅ ГЕНА УСПЕШНО ПОДКЛЮЧЕН!")

    @client.on(events.NewMessage())
    async def handler(event):
        # Логика ТЕМ
        if event.is_private:
            sender = await event.get_sender()
            phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
            if phone in MANAGERS: return
            
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute('SELECT topic_id FROM topics WHERE phone=?', (phone,)) as c:
                    row = await c.fetchone()
                    if row: tid = row[0]
                    else:
                        try:
                            res = await client(functions.channels.CreateForumTopicRequest(channel=GROUP_ID, title=f"{phone}"))
                            tid = res.updates[0].id
                            await db.execute('INSERT INTO topics VALUES (?,?)', (tid, phone))
                            await db.commit()
                        except: tid = None
            await client.send_message(GROUP_ID, f"От {phone}:\n{event.text}", reply_to=tid)

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

@app.route('/health')
async def health(): return "OK"

if __name__ == '__main__':
    print("!!! ВНИМАНИЕ: ЗАПУСК НОВОЙ ВЕРСИИ !!!")
    app.run(host='0.0.0.0', port=5000)
