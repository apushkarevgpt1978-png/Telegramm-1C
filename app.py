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

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, client_name TEXT, tg_client_id INTEGER,
                sender_number TEXT, messenger TEXT DEFAULT 'tg', message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER,
                direction TEXT, error_text TEXT, created_at DATETIME
            )
        """)
        await db.commit()

# --- ЛОГИРОВАНИЕ (СТРОГО 13 ПАРАМЕТРОВ) ---
async def log_to_db(source, phone, text, sender=None, f_url=None, messenger='tg', 
                    status='pending', direction='out', tg_id=None, error=None, 
                    client_name=None, tg_client_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, client_name, tg_client_id, sender_number, messenger, 
             message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, phone, client_name, tg_client_id, sender, messenger, 
              text, f_url, status, direction, tg_id, error, datetime.now()))
        await db.commit()

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
        raw_text = (event.raw_text or "").strip()
        
        if s_phone in managers_list:
            match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
            if match:
                target, msg = match.group(1).strip(), match.group(2).strip()
                try:
                    f_url = await save_tg_media(event)
                    if f_url: sent = await tg.send_file(target, f_url, caption=msg)
                    else: sent = await tg.send_message(target, msg)
                    await log_to_db("Manager", target, msg, sender=s_phone, f_url=f_url, direction="out", tg_id=sent.id)
                    await event.reply(f"✅ Отправлено клиенту {target}")
                except Exception as e: await event.reply(f"❌ Ошибка: {str(e)}")
            else:
                await event.reply("⚠️ Формат: `#79001234567/Текст`", parse_mode='markdown')
        else:
            name = f"{getattr(sender, 'first_name','')} {getattr(sender, 'last_name','')}".strip()
            f_url = await save_tg_media(event)
            await log_to_db("Client", s_phone, raw_text or "[Файл]", sender=s_phone, f_url=f_url, client_name=name, direction="in")

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

# --- ЭНДПОИНТЫ ---
@app.route('/fetch_new', methods=['GET', 'POST']) # РЕШЕНИЕ ТВОЕЙ ОШИБКИ 405
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        if rows:
            ids = [r['id'] for r in rows]
            await db.execute(f"UPDATE outbound_logs SET status='delivered_to_1c' WHERE id IN ({','.join(['?']*len(ids))})", ids)
            await db.commit()
        return jsonify(rows)

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone, f_url = str(data.get("phone", "")).lstrip('+').strip(), data.get("file")
    if not phone or not f_url: return jsonify({"error": "phone and file required"}), 400
    tg = await get_client()
    try:
        sent = await tg.send_file(phone, f_url, caption=data.get("text", ""))
        await log_to_db("1C", phone, data.get("text", ""), sender="system_1c", f_url=f_url, tg_id=sent.id)
        return jsonify({"status": "pending", "tg_id": sent.id}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/get_file/<filename>')
async def get_file(filename): return await send_from_directory(FILES_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
