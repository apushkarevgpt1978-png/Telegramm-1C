import os
import asyncio
import aiosqlite
import re
from datetime import datetime
from quart import Quart, request, jsonify, send_file
from telethon import TelegramClient, events

app = Quart(__name__)

# --- НАСТРОЙКИ ---
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
MANAGERS = os.environ.get('MANAGERS_PHONES', '').split(',')

client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.connect()
    return client

async def init_db():
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
                direction TEXT,
                error_text TEXT,
                created_at DATETIME,
                sent_at DATETIME
            )
        """)
        await db.commit()

async def log_to_db(source, phone, text, status='pending', direction='out', tg_id=None, error=None, file_url=None):
    async with aiosqlite.connect(DB_PATH) as db:
        # Здесь статус по умолчанию изменен на 'pending' для надежности
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, message_text, file_url, status, direction, tg_message_id, error_text, created_at, sent_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            source,          # 1
            phone,           # 2
            text,            # 3
            file_url,        # 4
            status,          # 5
            direction,       # 6
            tg_id,           # 7
            error,           # 8
            datetime.now(),  # 9
            datetime.now() if status in ['sent', 'received', 'delivered_to_1c'] else None # 10
        ))
        await db.commit()

# --- СЛУШАТЕЛЬ СОБЫТИЙ ---
async def start_listener():
    tg = await get_client()
    managers_list = [m.strip() for m in MANAGERS if m.strip()]

    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private:
            return 

        sender = await event.get_sender()
        sender_phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        raw_text = event.raw_text.strip()
        
        # 1. Если пишет МЕНЕДЖЕР
        if sender_phone in managers_list:
            match = re.match(r'^#(\d+)/(.*)', raw_text, re.DOTALL)
            if match:
                target_phone = match.group(1).strip()
                message_to_send = match.group(2).strip()
                try:
                    sent = await tg.send_message(target_phone, message_to_send)
                    # ВАЖНО: Ставим pending, чтобы 1С зафиксировала ответ менеджера
                    await log_to_db("Manager", target_phone, message_to_send, status="pending", direction="out", tg_id=sent.id)
                    await event.reply(f"✅ Отправлено клиенту {target_phone}")
                except Exception as e:
                    await event.reply(f"❌ Ошибка отправки: {str(e)}")
            else:
                await event.reply("⚠️ Ошибка формата! Используйте: `#номер/текст`")
        
        # 2. Если пишет КЛИЕНТ
        else:
            # ВАЖНО: Ставим pending, чтобы 1С могла забрать входящее
            await log_to_db("Client", sender_phone or "Unknown", raw_text, status="pending", direction="in", tg_id=event.id)

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

@app.route('/send', methods=['POST'])
async def send_telegram():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+')
    text = data.get("text", "")
    
    tg = await get_client()
    try:
        sent = await tg.send_message(phone, text)
        # МЕНЯЕМ ТУТ: вместо status="sent" ставим status="pending"
        # чтобы твоя новая процедура в 1С могла зафиксировать это событие
        await log_to_db("1C", phone, text, status="pending", direction="out", tg_id=sent.id)
        
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        await log_to_db("1C", phone, text, status="error", error=str(e))
        return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as cursor:
            rows = await cursor.fetchall()
            data = [dict(row) for row in rows]
        
        if not data:
            return jsonify([])

        ids = [row['id'] for row in data]
        placeholders = ', '.join(['?'] * len(ids))
        await db.execute(f"UPDATE outbound_logs SET status = 'delivered_to_1c' WHERE id IN ({placeholders})", ids)
        await db.commit()
        return jsonify(data)

@app.route('/debug_db', methods=['GET'])
async def debug_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs ORDER BY id DESC LIMIT 100") as cursor:
            return jsonify([dict(row) for row in await cursor.fetchall()])

# --- АВТОРИЗАЦИЯ (без изменений) ---
@app.route('/auth_phone', methods=['POST'])
async def auth_phone():
    data = await request.get_json()
    tg = await get_client()
    res = await tg.send_code_request(data.get('phone'))
    return jsonify({"hash": res.phone_code_hash})

@app.route('/auth_code', methods=['POST'])
async def auth_code():
    data = await request.get_json()
    tg = await get_client()
    await tg.sign_in(data.get('phone'), data.get('code'), phone_code_hash=data.get('hash'))
    return jsonify({"status": "success"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
