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

async def log_to_db(source, phone, text, status='sent', direction='out', tg_id=None, error=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, message_text, status, direction, tg_message_id, error_text, created_at, sent_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, phone, text, status, direction, tg_id, error, datetime.now(), datetime.now()))
        await db.commit()

# --- СЛУШАТЕЛЬ СОБЫТИЙ ---
async def start_listener():
    tg = await get_client()
    
    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        sender = await event.get_sender()
        sender_phone = getattr(sender, 'phone', '').lstrip('+')
        raw_text = event.raw_text.strip()

        # 1. Если пишет МЕНЕДЖЕР
        if sender_phone in MANAGERS:
            # Маска: #номер/текст
            match = re.match(r'^#(\d+)/(.*)', raw_text, re.DOTALL)
            
            if match:
                target_phone = match.group(1)
                message_to_send = match.group(2).strip()
                
                if not message_to_send:
                    await event.reply("⚠️ Вы забыли ввести текст сообщения после `/`")
                    return

                try:
                    sent = await tg.send_message(target_phone, message_to_send)
                    await log_to_db("Manager", target_phone, message_to_send, direction="out", tg_id=sent.id)
                    await event.reply(f"✅ Отправлено клиенту {target_phone}")
                except Exception as e:
                    await event.reply(f"❌ Ошибка отправки: {str(e)}")
            else:
                # Менеджер ошибся в маске
                await event.reply(
                    "⚠️ Ошибка формата!\n\n"
                    "Используйте: `#номер/текст`\n"
                    "Пример: `#79153019495/Добрый день!`"
                )
        
        # 2. Если пишет КЛИЕНТ (и его номера нет в списке менеджеров)
        else:
            await log_to_db("Client", sender_phone or "Unknown", raw_text, status="received", direction="in", tg_id=event.id)

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
        await log_to_db("1C", phone, text, status="sent", direction="out", tg_id=sent.id)
        return jsonify({"status": "sent", "tg_id": sent.id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/debug_db', methods=['GET'])
async def debug_db():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs ORDER BY id DESC LIMIT 100") as cursor:
            return jsonify([dict(row) for row in await cursor.fetchall()])

# --- АВТОРИЗАЦИЯ ---
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
