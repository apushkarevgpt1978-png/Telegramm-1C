import os, asyncio, aiosqlite, re, uuid
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
BASE_URL = 'http://192.168.121.99:5000/get_file' # Проверь свой IP

if not os.path.exists(FILES_DIR): os.makedirs(FILES_DIR)

async def get_client():
    if not hasattr(app, 'tg_client'):
        app.tg_client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await app.tg_client.connect()
    return app.tg_client

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, sender_number TEXT,
                client_id TEXT, client_name TEXT, messenger TEXT DEFAULT 'tg',
                message_text TEXT, file_url TEXT, status TEXT DEFAULT 'pending',
                tg_message_id INTEGER, direction TEXT, error_text TEXT, created_at DATETIME
            )
        """)
        cursor = await db.execute("PRAGMA table_info(outbound_logs)")
        cols = [row[1] for row in await cursor.fetchall()]
        if 'client_id' not in cols: await db.execute("ALTER TABLE outbound_logs ADD COLUMN client_id TEXT")
        if 'client_name' not in cols: await db.execute("ALTER TABLE outbound_logs ADD COLUMN client_name TEXT")
        await db.commit()

async def log_to_db(source, phone, text, sender=None, f_url=None, c_id=None, c_name=None, status='pending', direction='out', tg_id=None, error=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, sender_number, client_id, client_name, messenger, message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, phone, sender, c_id, c_name, 'tg', text, f_url, status, direction, tg_id, error, datetime.now()))
        await db.commit()

async def save_tg_media(event):
    if event.message.media:
        file_ext = ".jpg" # По умолчанию
        if hasattr(event.message.media, 'document'):
            for attr in event.message.media.document.attributes:
                if hasattr(attr, 'file_name'):
                    file_ext = os.path.splitext(attr.file_name)[1]
        filename = f"{uuid.uuid4()}{file_ext}"
        path = os.path.join(FILES_DIR, filename)
        await event.message.download_media(file=path)
        return f"{BASE_URL}/{filename}"
    return None

# --- СЛУШАТЕЛЬ ---
async def start_listener():
    tg = await get_client()
    managers_list = [m.strip() for m in MANAGERS if m.strip()]

    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private: return
        sender = await event.get_sender()
        phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        raw_text = event.raw_text.strip()

        # --- ЛОГИКА МЕНЕДЖЕРА ---
        if phone in managers_list:
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
                    
                    await log_to_db("Manager", target_phone, msg_text, sender=phone, f_url=f_url, direction="out", tg_id=sent.id)
                    await event.reply("✅ Отправлено клиенту")
                except Exception as e:
                    await event.reply(f"❌ Ошибка отправки: {str(e)}")
            else:
                # Если менеджер написал без маски
                await event.reply("⚠️ Чтобы отправить сообщение клиенту, используйте формат:\n`#79001234567/Текст сообщения`")

        # --- ЛОГИКА КЛИЕНТА ---
        else:
            name = f"{getattr(sender, 'first_name','')} {getattr(sender, 'last_name','')}".strip() or "User"
            f_url = await save_tg_media(event)
            await log_to_db("Client", phone, raw_text or "[Файл]", sender=phone, f_url=f_url, c_name=name, direction="in")

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

@app.route('/get_file/<filename>')
async def get_file(filename):
    return await send_from_directory(FILES_DIR, filename)

@app.route('/send_file', methods=['POST'])
async def send_file_from_1c():
    data = await request.get_json()
    if not data: return jsonify({"error": "Empty JSON"}), 400
    phone = str(data.get("phone", "")).lstrip('+').strip()
    file_url = data.get("file")
    caption = data.get("text", "")
    c_id = data.get("client_id")
    c_name = data.get("client_name")

    if not phone or not file_url:
        return jsonify({"error": "phone and file are required"}), 400

    tg = await get_client()
    try:
        sent = await tg.send_file(phone, file_url, caption=caption)
        await log_to_db("1C", phone, caption, sender="system_1c", f_url=file_url, c_id=c_id, c_name=c_name, tg_id=sent.id)
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as c:
            rows = [dict(r) for r in await c.fetchall()]
        if rows:
            ids = [r['id'] for r in rows]
            await db.execute(f"UPDATE outbound_logs SET status='delivered_to_1c' WHERE id IN ({','.join(['?']*len(ids))})", ids)
            await db.commit()
        return jsonify(rows)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
