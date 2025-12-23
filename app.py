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
BASE_URL = 'http://192.168.121.99:5000/get_file'

if not os.path.exists(FILES_DIR): os.makedirs(FILES_DIR)

client = None
async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.connect()
    return client

# --- БД (Добавили client_id и client_name) ---
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
        await db.commit()

# --- ЛОГИРОВАНИЕ (13 ПАРАМЕТРОВ) ---
async def log_to_db(source, phone, text, sender=None, f_url=None, c_id=None, c_name=None, status='pending', direction='out', tg_id=None, error=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, sender_number, client_id, client_name, messenger, message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, 'tg', ?, ?, ?, ?, ?, ?, ?)
        """, (source, phone, sender, c_id, c_name, text, f_url, status, direction, tg_id, error, datetime.now()))
        await db.commit()

# --- СОХРАНЕНИЕ МЕДИА ДЛЯ КЛИЕНТОВ ---
async def save_tg_media(event):
    if event.message.media:
        file_ext = ".jpg"
        if hasattr(event.message.media, 'document'):
            for attr in event.message.media.document.attributes:
                if hasattr(attr, 'file_name'): file_ext = os.path.splitext(attr.file_name)[1]
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
        s_phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        
        if s_phone in managers_list:
            match = re.search(r'#(\d+)/(.*)', event.raw_text, re.DOTALL)
            if match:
                target, msg = match.group(1).strip(), match.group(2).strip()
                sent = await tg.send_message(target, msg) # Только текст, как вчера
                await log_to_db("Manager", target, msg, sender=s_phone, direction="out", tg_id=sent.id)
        else:
            name = f"{getattr(sender, 'first_name','')} {getattr(sender, 'last_name','')}".strip()
            f_url = await save_tg_media(event)
            await log_to_db("Client", s_phone, event.raw_text or "[Файл]", sender=s_phone, f_url=f_url, c_name=name, direction="in")

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

# --- API ЭНДПОИНТЫ ---

@app.route('/send', methods=['POST'])
async def send_text_from_1c():
    data = await request.get_json()
    if not data: return jsonify({"error": "Empty JSON"}), 400

    phone = str(data.get("phone", "")).lstrip('+').strip()
    text = data.get("text", "")
    c_id = data.get("client_id")
    c_name = data.get("client_name")

    if not phone or not text:
        return jsonify({"error": "phone and text are required"}), 400

    tg = await get_client()
    try:
        sent = await tg.send_message(phone, text)
        
        # Логируем (те же 13 параметров, что и в send_file)
        await log_to_db(
            source="1C", 
            phone=phone, 
            text=text, 
            sender="system_1c", 
            c_id=c_id, 
            c_name=c_name, 
            status="pending", 
            direction="out", 
            tg_id=sent.id
        )
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone, f_url = str(data.get("phone", "")).lstrip('+').strip(), data.get("file")
    c_id, c_name = data.get("client_id"), data.get("client_name")
    tg = await get_client()
    try:
        sent = await tg.send_file(phone, f_url, caption=data.get("text", ""))
        await log_to_db("1C", phone, data.get("text", ""), sender="system_1c", f_url=f_url, c_id=c_id, c_name=c_name, tg_id=sent.id)
        return jsonify({"status": "pending"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as c:
            rows = [dict(r) for r in await c.fetchall()]
        if rows:
            ids = [r['id'] for r in rows]
            await db.execute(f"UPDATE outbound_logs SET status='ok' WHERE id IN ({','.join(['?']*len(ids))})", ids)
            await db.commit()
        return jsonify(rows)

@app.route('/get_file/<filename>')
async def get_file(filename): return await send_from_directory(FILES_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
