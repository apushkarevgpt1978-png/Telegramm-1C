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
BASE_URL = 'http://192.168.121.99:5000/get_file'

if not os.path.exists(FILES_DIR):
    os.makedirs(FILES_DIR)

client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.connect()
    return client

# --- ИНИЦИАЛИЗАЦИЯ БД ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                phone TEXT,
                client_name TEXT,
                tg_client_id INTEGER,
                sender_number TEXT,
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
        
        cursor = await db.execute("PRAGMA table_info(outbound_logs)")
        cols = [row[1] for row in await cursor.fetchall()]
        
        # Список новых колонок для миграции
        migrations = {
            'sender_number': "ALTER TABLE outbound_logs ADD COLUMN sender_number TEXT",
            'messenger': "ALTER TABLE outbound_logs ADD COLUMN messenger TEXT DEFAULT 'tg'",
            'direction': "ALTER TABLE outbound_logs ADD COLUMN direction TEXT",
            'file_url': "ALTER TABLE outbound_logs ADD COLUMN file_url TEXT",
            'status': "ALTER TABLE outbound_logs ADD COLUMN status TEXT DEFAULT 'pending'",
            'client_name': "ALTER TABLE outbound_logs ADD COLUMN client_name TEXT",
            'tg_client_id': "ALTER TABLE outbound_logs ADD COLUMN tg_client_id INTEGER"
        }
        
        for col, sql in migrations.items():
            if col not in cols:
                try:
                    await db.execute(sql)
                except:
                    pass
            
        await db.commit()

# --- ОБНОВЛЕННАЯ ФУНКЦИЯ ЛОГИРОВАНИЯ ---
async def log_to_db(source, phone, text, sender=None, f_url=None, messenger='tg', 
                    status='pending', direction='out', tg_id=None, error=None, 
                    client_name=None, tg_client_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, client_name, tg_client_id, sender_number, messenger, 
             message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source, phone, client_name, tg_client_id, sender, messenger, 
            text, f_url, status, direction, tg_id, error, datetime.now()
        ))
        await db.commit()

# --- ФУНКЦИЯ СОХРАНЕНИЯ ФАЙЛА ---
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
    print(f"DEBUG: ГЕНА СЛУШАЕТ. Менеджеры: {managers_list}")

    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private: return 

        sender = await event.get_sender()
        sender_phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        
        # Получаем имя (Имя + Фамилия или Username)
        first_name = getattr(sender, 'first_name', '') or ''
        last_name = getattr(sender, 'last_name', '') or ''
        full_name = f"{first_name} {last_name}".strip() or getattr(sender, 'username', 'Unknown')
        tg_client_id = getattr(sender, 'id', None)
        
        raw_text = event.raw_text.strip()
        
        # --- БЛОК ПРОВЕРКИ МЕНЕДЖЕРА ---
        if sender_phone in managers_list:
    # Используем re.search вместо re.match, если нужно игнорировать пробелы в начале
    match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
    
    if match:
        target_phone = match.group(1).strip()
        message_to_send = match.group(2).strip()
        
        try:
            f_url = await save_tg_media(event)
            
            if f_url:
                local_path = os.path.join(FILES_DIR, f_url.split('/')[-1])
                sent = await tg.send_file(target_phone, local_path, caption=message_to_send)
            else:
                sent = await tg.send_message(target_phone, message_to_send)
            
            # Логирование (без изменений)
            await log_to_db(
                source="Manager", phone=target_phone, text=message_to_send, 
                sender=sender_phone, f_url=f_url, direction="out", tg_id=sent.id
            )
            await event.reply(f"✅ Отправлено клиенту {target_phone}")
            
        except Exception as e:
            await event.reply(f"❌ Ошибка отправки: {str(e)}")
    else:
        # --- ИСПРАВЛЕННЫЙ БЛОК ОШИБКИ ---
        # Оборачиваем пример в обратные кавычки (backticks) для копирования
        # Добавляем parse_mode='md' или 'html', чтобы Telegram понял форматирование
        example_mask = f"`#79876543210/текст сообщения`"
        
        error_message = (
            "⚠️ **Ошибка формата!**\n\n"
            "Чтобы отправить сообщение клиенту, используйте маску:\n"
            f"{example_mask}\n\n"
            "*(Нажмите на маску выше, чтобы скопировать её, затем вставьте и замените номер и текст)*"
        )
        
        # Важно: 
        await event.reply(error_message, parse_mode='markdown')
        
        else:
            f_url = await save_tg_media(event)
            await log_to_db(
                source="Client", 
                phone=sender_phone or "Unknown", 
                client_name=full_name,
                tg_client_id=tg_client_id,
                text=raw_text or "[Файл]", 
                sender=sender_phone, 
                f_url=f_url,
                direction="in", 
                status="pending"
            )

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

# --- API ЭНДПОИНТЫ ---

@app.route('/get_file/<filename>')
async def get_file(filename):
    return await send_from_directory(FILES_DIR, filename)

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

@app.route('/send', methods=['POST'])
async def send_telegram():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+')
    text = data.get("text", "")
    tg = await get_client()
    try:
        sent = await tg.send_message(phone, text)
        await log_to_db("1C", phone, text, sender="system_1c", direction="out", 
                        tg_id=sent.id, tg_client_id=sent.peer_id.user_id if hasattr(sent.peer_id, 'user_id') else None)
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        await log_to_db("1C", phone, text, sender="system_1c", status="error", error=str(e))
        return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file_from_1c():
    data = await request.get_json()
    if not data:
        return jsonify({"error": "Empty JSON"}), 400

    phone = str(data.get("phone", "")).lstrip('+').strip()
    file_url = data.get("file")
    caption = data.get("text", "")

    if not phone or not file_url:
        return jsonify({"error": "phone and file are required"}), 400

    tg = await get_client()
    try:
        sent = await tg.send_file(phone, file_url, caption=caption)
        await log_to_db(
            source="1C", 
            phone=phone, 
            text=caption or "Файл", 
            sender="system_1c", 
            f_url=file_url, 
            status="pending",
            direction="out", 
            tg_id=sent.id,
            tg_client_id=sent.peer_id.user_id if hasattr(sent.peer_id, 'user_id') else None
        )
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/debug_db', methods=['GET'])
async def debug_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs ORDER BY id DESC LIMIT 20") as cursor:
            return jsonify([dict(row) for row in await cursor.fetchall()])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
