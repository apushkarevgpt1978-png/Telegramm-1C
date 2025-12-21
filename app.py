from quart import Quart, request, jsonify, send_file
import os
import hashlib
import mimetypes
import asyncio
import aiosqlite
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

app = Quart(__name__)

# --- НАСТРОЙКИ (через переменные окружения) ---
API_ID = os.getenv('TG_API_ID')
API_HASH = os.getenv('TG_API_HASH')
SESSION_PATH = os.getenv('TG_SESSION_PATH', '/app/GenaAPI')
TEMP_STORAGE = os.getenv('TG_TEMP_STORAGE', '/tmp/telegram_files')
DB_PATH = os.getenv('TG_DB_PATH', 'gateway_messages.db')

os.makedirs(TEMP_STORAGE, exist_ok=True)

# --- БЛОК РАБОТЫ С БАЗОЙ ДАННЫХ ---

async def init_db():
    """Создание таблицы для логов при запуске приложения"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                phone TEXT,
                message_text TEXT,
                file_url TEXT,
                status TEXT,
                tg_message_id INTEGER,
                error_text TEXT,
                created_at DATETIME,
                sent_at DATETIME
            )
        """)
        await db.commit()

async def log_to_db(source, phone, text, file_url=None):
    """Первичная фиксация запроса в статусе pending"""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, message_text, file_url, status, created_at) 
            VALUES (?, ?, ?, ?, ?, ?)
        """, (source, phone, text, file_url, 'pending', datetime.now()))
        row_id = cursor.lastrowid
        await db.commit()
        return row_id

async def update_log_status(row_id, status, tg_id=None, error=None):
    """Обновление записи результатом отправки"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE outbound_logs 
            SET status = ?, tg_message_id = ?, error_text = ?, sent_at = ? 
            WHERE id = ?
        """, (status, tg_id, error, datetime.now() if status == 'sent' else None, row_id))
        await db.commit()

# --- КЛИЕНТ ТЕЛЕГРАМ ---

async def get_client():
    # Добавляем получение данных из Portainer
    api_id = int(os.environ.get('API_ID'))
    api_hash = os.environ.get('API_HASH')
    session_path = os.environ.get('TG_SESSION_PATH')
    
    # Создаем клиент, используя эти данные
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    return client

async def process_media(client, message):
    if not message.media:
        return None
    try:
        file_hash = hashlib.md5(f"{message.id}_{message.date.timestamp()}".encode()).hexdigest()[:8]
        mime = None
        if hasattr(message.media, 'document'):
            mime = message.media.document.mime_type
        elif hasattr(message.media, 'photo'):
            mime = "image/jpeg"
            
        extension = mimetypes.guess_extension(mime) if mime else ""
        if not extension and mime == "image/webapp": extension = ".png"

        attr_name = "file"
        if hasattr(message.media, 'document'):
            for attr in message.media.document.attributes:
                if hasattr(attr, 'file_name'):
                    attr_name = os.path.splitext(attr.file_name)[0]
                    break
        
        filename = f"{file_hash}_{attr_name}{extension}"
        save_path = os.path.join(TEMP_STORAGE, filename)
        path = await client.download_media(message, file=save_path)
        
        if path:
            return {
                "url": f"http://{request.host}/download/{filename}",
                "filename": filename,
                "filesize": os.path.getsize(path),
                "mimetype": mime or "application/octet-stream"
            }
    except Exception as e:
        print(f"Media error: {e}")
    return None

# --- ЭНДПОИНТЫ ---

@app.route('/send', methods=['POST'])
async def send_telegram():
    data = await request.get_json()
    phone = data.get("phone", "").strip()
    text = data.get("text", "").strip()
    source = data.get("source", "1C")

    # Регистрация в БД перед отправкой
    db_row_id = await log_to_db(source, phone, text)

    client = await get_client()
    try:
        if not await client.is_user_authorized():
            await update_log_status(db_row_id, "error", error="Auth required")
            return jsonify({"error": "Auth required"}), 401
        
        sent = await client.send_message(phone, text)
        await update_log_status(db_row_id, "sent", tg_id=sent.id)
        
        return jsonify({"status": "sent", "db_id": db_row_id, "tg_id": sent.id}), 200
    except Exception as e:
        await update_log_status(db_row_id, "error", error=str(e))
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        await client.disconnect()

@app.route('/send_url', methods=['POST'])
async def send_url_telegram():
    data = await request.get_json()
    phone = data.get("phone", "").strip()
    file_url = data.get("file_url", "").strip()
    caption = data.get("caption", "").strip()
    source = data.get("source", "1C")

    # Регистрация в БД перед отправкой
    db_row_id = await log_to_db(source, phone, caption, file_url=file_url)

    client = await get_client()
    try:
        if not await client.is_user_authorized():
            await update_log_status(db_row_id, "error", error="Auth required")
            return jsonify({"error": "Auth required"}), 401
        
        sent = await client.send_file(phone, file_url, caption=caption)
        await update_log_status(db_row_id, "sent", tg_id=sent.id)
        
        return jsonify({"status": "sent", "db_id": db_row_id, "tg_id": sent.id}), 200
    except Exception as e:
        await update_log_status(db_row_id, "error", error=str(e))
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        await client.disconnect()

@app.route('/get_messages', methods=['POST'])
async def get_messages():
    data = await request.get_json() or {}
    limit_dialogs = int(data.get("limit_dialogs", 10))
    only_unread = data.get("only_unread", False)
    client = await get_client()
    try:
        if not await client.is_user_authorized():
            return jsonify({"error": "Auth required"}), 401
        all_messages = []
        async for dialog in client.iter_dialogs(limit=limit_dialogs):
            if not dialog.is_user or dialog.id == 777000: continue
            count_to_fetch = dialog.unread_count if only_unread else int(data.get("limit_messages", 5))
            if count_to_fetch == 0 and only_unread: continue
            read_max_id = getattr(dialog.dialog, 'read_inbox_max_id', 0)
            async for msg in client.iter_messages(dialog.id, limit=count_to_fetch):
                media_data = await process_media(client, msg)
                all_messages.append({
                    "message_id": msg.id,
                    "sender_id": msg.sender_id,
                    "chat_id": dialog.id,
                    "chat_name": dialog.name,
                    "text": msg.text or "",
                    "date": msg.date.isoformat(),
                    "is_out": msg.out,
                    "is_unread": (not msg.out) and (msg.id > read_max_id),
                    "media_info": media_data
                })
        return jsonify({"status": "success", "messages": all_messages}), 200
    finally:
        await client.disconnect()

@app.route('/download/<filename>', methods=['GET'])
async def download_file(filename):
    path = os.path.join(TEMP_STORAGE, filename)
    if os.path.exists(path): return await send_file(path)
    return "File not found", 404

@app.before_serving
async def setup():
    """Запуск инициализации БД перед началом работы сервера"""
    await init_db()

@app.route('/debug_db', methods=['GET'])
async def debug_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs ORDER BY id DESC LIMIT 50") as cursor:
            rows = await cursor.fetchall()
            # Превращаем в список словарей
            data = [dict(row) for row in rows]
            # Отправляем именно как JSON-ответ
            return jsonify(data)

@app.route('/auth_phone', methods=['POST'])
async def auth_phone():
    # Добавляем эти строки, чтобы функция видела настройки из Portainer:
    api_id = int(os.environ.get('API_ID'))
    api_hash = os.environ.get('API_HASH')
    session_path = os.environ.get('TG_SESSION_PATH')
    
    data = await request.get_json()
    phone = data.get('phone')
    
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    send_code = await client.send_code_request(phone)
    await client.disconnect()
    return jsonify({"status": "code_sent", "phone_code_hash": send_code.phone_code_hash})

@app.route('/auth_code', methods=['POST'])
async def auth_code():
    api_id = int(os.environ.get('API_ID'))
    api_hash = os.environ.get('API_HASH')
    session_path = os.environ.get('TG_SESSION_PATH')
    
    data = await request.get_json()
    phone = data.get('phone')
    code = data.get('code')
    hash = data.get('hash')
    
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    await client.sign_in(phone, code, phone_code_hash=hash)
    me = await client.get_me()
    await client.disconnect()
    return jsonify({"status": "success", "user": me.first_name})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
