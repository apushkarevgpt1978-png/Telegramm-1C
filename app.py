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

async def get_client():
    if not hasattr(app, 'tg_client'):
        app.tg_client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await app.tg_client.connect()
    return app.tg_client

# --- БЕЗОПАСНАЯ ИНИЦИАЛИЗАЦИЯ БД ---
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
        # Проверка колонок (миграция)
        cursor = await db.execute("PRAGMA table_info(outbound_logs)")
        cols = [row[1] for row in await cursor.fetchall()]
        if 'client_id' not in cols: await db.execute("ALTER TABLE outbound_logs ADD COLUMN client_id TEXT")
        if 'client_name' not in cols: await db.execute("ALTER TABLE outbound_logs ADD COLUMN client_name TEXT")
        await db.commit()

# --- ЛОГИРОВАНИЕ (СТРОГО 13 ЗНАЧЕНИЙ) ---
async def log_to_db(source, phone, text, sender=None, f_url=None, c_id=None, c_name=None, status='pending', direction='out', tg_id=None, error=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO outbound_logs 
            (source, phone, sender_number, client_id, client_name, messenger, message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, phone, sender, c_id, c_name, 'tg', text, f_url, status, direction, tg_id, error, datetime.now()))
        await db.commit()

# --- СЛУШАТЕЛЬ ---
async def start_listener():
    tg = await get_client()
    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private: return
        sender = await event.get_sender()
        phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        if phone in [m.strip() for m in MANAGERS if m.strip()]:
            match = re.match(r'^#(\d+)/(.*)', event.raw_text, re.DOTALL)
            if match:
                target, msg = match.group(1).strip(), match.group(2).strip()
                sent = await tg.send_message(target, msg)
                await log_to_db("Manager", target, msg, sender=phone, direction="out", tg_id=sent.id)
        else:
            name = f"{getattr(sender, 'first_name','')} {getattr(sender, 'last_name','')}".strip()
            await log_to_db("Client", phone, event.raw_text, sender=phone, c_name=name, direction="in")

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone, f_url = str(data.get("phone", "")).lstrip('+'), data.get("file")
    c_id, c_name, text = data.get("client_id"), data.get("client_name"), data.get("text", "")
    tg = await get_client()
    try:
        sent = await tg.send_file(phone, f_url, caption=text)
        await log_to_db("1C", phone, text, sender="system_1c", f_url=f_url, c_id=c_id, c_name=c_name, tg_id=sent.id)
        return jsonify({"status": "pending"}), 200
    except Exception as e:
        return jsonify({"error": str(e), "url": f_url}), 500

@app.route('/fetch_new', methods=['GET'])
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
