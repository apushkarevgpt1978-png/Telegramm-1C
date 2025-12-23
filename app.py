import os
import asyncio
import aiosqlite
import re
import uuid
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events

app = Quart(__name__)

# --- НАСТРОЙКИ ---
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
MANAGERS = os.environ.get('MANAGERS_PHONES', '').split(',')

FILES_DIR = '/app/files'
BASE_URL = 'http://192.168.121.99:5000/get_file' # Укажи актуальный IP

if not os.path.exists(FILES_DIR):
    os.makedirs(FILES_DIR)

client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.connect()
    return client

# --- ИНИЦИАЛИЗАЦИЯ БД (С УДАЛЕНИЕМ СТАРОЙ) ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DROP TABLE IF EXISTS outbound_logs")
        await db.execute("""
            CREATE TABLE outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                phone TEXT,
                sender_number TEXT,
                client_id TEXT,
                client_name TEXT,
                messenger TEXT DEFAULT 'tg',
                message_text TEXT,
                file_url TEXT,
                status TEXT DEFAULT 'pending',
                tg_message_id INTEGER,
                direction TEXT,
                error_text TEXT,
                created_at DATETIME
            )
        """)
        await db.commit()
        print("DEBUG: База данных пересоздана (client_id, client_name добавлены)")

# --- УНИВЕРСАЛЬНАЯ ФУНКЦИЯ ЛОГИРОВАНИЯ (13 ЗНАЧЕНИЙ) ---
async def log_to_db(source, phone, text, sender=None, f_url=None, c_id=None, c_name=None, status='pending', direction='out', tg_id=None, error=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, sender_number, client_id, client_name, messenger, message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, 'tg', ?, ?, ?, ?, ?, ?, ?)
        """, (
            source, phone, sender, c_id, c_name, text, f_url, status, direction, tg_id, error, datetime.now()
        ))
        await db.commit()

# --- СОХРАНЕНИЕ МЕДИА ---
async def save_tg_media(event):
    if event.message.media:
        file_ext = ".jpg"
        if hasattr(event.message.media, 'document'):
            for attr in event.message.media.document.attributes:
                if hasattr(attr, 'file_name'):
                    file_ext = os.path.splitext(attr.file_name)[1]
        
        filename = f"{uuid.uuid4()}{file_ext}"
        path = os.path.join(FILES_DIR, filename)
        await event.message.download_media(file=path)
        return f"{BASE_URL}/{filename}"
    return None

# --- СЛУШАТЕЛЬ ТЕЛЕГРАМ ---
async def start_listener():
    tg = await get_client()
    managers_list = [m.strip() for m in MANAGERS if m.strip()]

    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private: return 

        sender = await event.get_sender()
        sender_phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        raw_text = event.raw_text.strip()
        
        # 1. МЕНЕДЖЕР
        if sender_phone in managers_list:
            match = re.match(r'^#(\d+)/(.*)', raw_text, re.DOTALL)
            if match:
                target_phone = match.group(1).strip()
                msg_text = match.group(2).strip()
                try:
                    f_url = await save_tg_media(event)
                    if f_url:
                        sent = await tg.send_file(target_phone, f_url, caption=msg_text)
                    else:
                        sent = await tg.send_message(target_phone, msg_text)
                    
                    await log_to_db(source="Manager", phone=target_phone, text=msg_text, sender=sender_phone, f_url=f_url, direction="out", tg_id=sent.id)
                    await event.reply("✅ Отправлено")
                except Exception as e:
                    await event.reply(f"❌ Ошибка: {str(e)}")
        
        # 2. КЛИЕНТ
        else:
            # Получаем имя из профиля TG
            first_name = getattr(sender, 'first_name', '') or ''
            last_name = getattr(sender, 'last_name', '') or ''
            full_name = f"{first_name} {last_name}".strip() or "Unknown TG User"
            
            f_url = await save_tg_media(event)
            await log_to_db(
                source="Client", 
                phone=sender_phone or "Unknown", 
                text=raw_text or "[Файл]", 
                sender=sender_phone, 
                f_url=f_url,
                c_name=full_name,
                direction="in"
            )

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

# --- API ЭНДПОИНТЫ ---

@app.route('/get_file/<filename>')
async def get_file(filename):
    return await send_from_directory(FILES_DIR, filename)

@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+')
    text = data.get("text", "")
    c_id = data.get("client_id")
    c_name = data.get("client_name")
    
    tg = await get_client()
    try:
        sent = await tg.send_message(phone, text)
        await log_to_db("1C", phone, text, sender="system_1c", c_id=c_id, c_name=c_name, direction="out", tg_id=sent.id)
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+')
    file_url = data.get("file")
    text = data.get("text", "")
    c_id = data.get("client_id")
    c_name = data.get("client_name")
    
    tg = await get_client()
    try:
        sent = await tg.send_file(phone, file_url, caption=text)
        await log_to_db("1C", phone, text, sender="system_1c", f_url=file_url, c_id=c_id, c_name=c_name, direction="out", tg_id=sent.id)
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as cursor:
            rows = await cursor.fetchall()
            data = [dict(row) for row in rows]
        if data:
            ids = [row['id'] for row in data]
            placeholders = ', '.join(['?'] * len(ids))
            await db.execute(f"UPDATE outbound_logs SET status = 'delivered_to_1c' WHERE id IN ({placeholders})", ids)
            await db.commit()
        return jsonify(data)

@app.route('/debug_db', methods=['GET'])
async def debug_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs ORDER BY id DESC LIMIT 50") as cursor:
            return jsonify([dict(row) for row in await cursor.fetchall()])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
