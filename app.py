import os
import asyncio
import hashlib
import mimetypes
import aiosqlite
from datetime import datetime
from quart import Quart, request, jsonify, send_file
from telethon import TelegramClient, events

app = Quart(__name__)

# --- НАСТРОЙКИ ---
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
TEMP_STORAGE = os.environ.get('TG_TEMP_STORAGE', '/tmp/telegram_files')
MANAGERS = os.environ.get('MANAGERS_PHONES', '').split(',')

os.makedirs(TEMP_STORAGE, exist_ok=True)

# Глобальный клиент
client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.connect()
    return client

# --- РАБОТА С БАЗОЙ ---

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Создаем таблицу с учетом новых полей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                phone TEXT,
                message_text TEXT,
                file_url TEXT,
                status TEXT,
                tg_message_id INTEGER,
                direction TEXT,
                error_text TEXT,
                created_at DATETIME,
                sent_at DATETIME
            )
        """)
        # Проверка и добавление колонки direction для старых баз
        cursor = await db.execute("PRAGMA table_info(outbound_logs)")
        cols = [row[1] for row in await cursor.fetchall()]
        if 'direction' not in cols:
            await db.execute("ALTER TABLE outbound_logs ADD COLUMN direction TEXT DEFAULT 'out'")
        await db.commit()

async def log_to_db(source, phone, text, file_url=None, status='pending', direction='out', tg_id=None, error=None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, message_text, file_url, status, direction, tg_message_id, error_text, created_at, sent_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, phone, text, file_url, status, direction, tg_id, error, datetime.now(), datetime.now() if status == 'sent' else None))
        row_id = cursor.lastrowid
        await db.commit()
        return row_id

# --- ОБРАБОТКА МЕДИА ---

async def process_media(message):
    if not message.media: return None
    try:
        file_hash = hashlib.md5(f"{message.id}_{message.date.timestamp()}".encode()).hexdigest()[:8]
        mime = message.media.document.mime_type if hasattr(message.media, 'document') else "image/jpeg"
        ext = mimetypes.guess_extension(mime) or ".bin"
        filename = f"{file_hash}_{message.id}{ext}"
        save_path = os.path.join(TEMP_STORAGE, filename)
        path = await client.download_media(message, file=save_path)
        return f"http://{request.host}/download/{filename}" if path else None
    except: return None

# --- СЛУШАТЕЛЬ СОБЫТИЙ ---

async def start_listener():
    tg = await get_client()
    
    @tg.on(events.NewMessage)
    async def handler(event):
        # Пропускаем сообщения, которые мы только что отправили через /send (они уже в базе)
        # Но ловим те, что отправлены ВРУЧНУЮ с телефона
        sender = await event.get_sender()
        phone = getattr(sender, 'phone', '').lstrip('+')
        
        # Определяем роль
        if event.out:
            if phone in MANAGERS:
                source, direction = "Manager", "out"
            else:
                return # Это сообщение от /send, игнорируем дубль
        else:
            source, direction = "Client", "in"

        # Сохраняем входящее или ручной ответ менеджера
        await log_to_db(
            source=source,
            phone=phone or "Unknown",
            text=event.raw_text,
            direction=direction,
            status="received",
            tg_id=event.id
        )

# --- ЭНДПОИНТЫ ---

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

@app.route('/send', methods=['POST'])
async def send_telegram():
    data = await request.get_json()
    phone, text = data.get("phone", ""), data.get("text", "")
    file_url = data.get("file_url") # Поддержка отправки файлов через основной эндпоинт
    
    tg = await get_client()
    try:
        if file_url:
            sent = await tg.send_file(phone, file_url, caption=text)
        else:
            sent = await tg.send_message(phone, text)
        
        await log_to_db("1C", phone, text, file_url=file_url, status="sent", tg_id=sent.id)
        return jsonify({"status": "sent", "tg_id": sent.id}), 200
    except Exception as e:
        await log_to_db("1C", phone, text, status="error", error=str(e))
        return jsonify({"error": str(e)}), 500

@app.route('/debug_db', methods=['GET'])
async def debug_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs ORDER BY id DESC LIMIT 100") as cursor:
            return jsonify([dict(row) for row in await cursor.fetchall()])

@app.route('/download/<filename>', methods=['GET'])
async def download_file(filename):
    path = os.path.join(TEMP_STORAGE, filename)
    return await send_file(path) if os.path.exists(path) else ("Not found", 404)

# --- АВТОРИЗАЦИЯ ---

@app.route('/auth_phone', methods=['POST'])
async def auth_phone():
    data = await request.get_json()
    tg = await get_client()
    res = await tg.send_code_request(data.get('phone'))
    return jsonify({"status": "code_sent", "hash": res.phone_code_hash})

@app.route('/auth_code', methods=['POST'])
async def auth_code():
    data = await request.get_json()
    tg = await get_client()
    await tg.sign_in(data.get('phone'), data.get('code'), phone_code_hash=data.get('hash'))
    me = await tg.get_me()
    return jsonify({"status": "success", "user": me.first_name})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
