import os, asyncio, aiosqlite, re, uuid
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events

app = Quart(__name__)

# --- НАСТРОЙКИ ---
API_ID = 23131386
API_HASH = '690d8745b7a329c084f2bb092e541002'
SESSION_PATH = '/app/data/GenaAPI'
GROUP_ID = -1003599844429
MANAGERS = ['79153019495']


DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')

FILES_DIR = '/app/files'
BASE_URL = os.environ.get('BASE_URL', 'http://192.168.121.99:5000')

if not os.path.exists(FILES_DIR): os.makedirs(FILES_DIR)

client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.start()
    return client

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, client_name TEXT, client_id TEXT,
                sender_number TEXT, messenger TEXT DEFAULT 'tg', message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER,
                direction TEXT, error_text TEXT, created_at DATETIME, manager TEXT
            )
        """)
        try:
            await db.execute("ALTER TABLE outbound_logs ADD COLUMN manager TEXT")
        except: pass 
        await db.commit()

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager=None, s_number=None, f_url=None, direction='in', tg_id=None):
    messenger = 'tg'
    created_at = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            # 13 полей в INSERT и 13 значений в VALUES
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, phone, c_name, c_id, manager, s_number, messenger, text, f_url, 'pending', direction, tg_id, created_at))
            await db.commit()
    except Exception as e:
        print(f"⚠️ ОШИБКА БД: {e}")

async def save_tg_media(event):
    if event.message.media:
        file_ext = ".jpg"
        if hasattr(event.message.media, 'document'):
            for attr in event.message.media.document.attributes:
                if hasattr(attr, 'file_name'): file_ext = os.path.splitext(attr.file_name)[1]
        filename = f"{uuid.uuid4()}{file_ext}"
        path = os.path.join(FILES_DIR, filename)
        await event.message.download_media(file=path)
        return f"{BASE_URL}/get_file/{filename}"
    return None

async def start_listener():
    tg = await get_client()
    managers_list = [m.strip() for m in MANAGERS if m.strip()]
    
    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private: return
        sender = await event.get_sender()
        s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
        s_full_name = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip() or "Unknown"
        s_id = str(event.sender_id)
        raw_text = (event.raw_text or "").strip()

        # 1. ОТПРАВКА МЕНЕДЖЕРОМ (ШЛЮЗ)
        if s_phone in managers_list:
            match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
            if match:
                target_phone, message_text = match.group(1).strip(), match.group(2).strip()
                try:
                    c_n, c_i = "Client", target_phone
                    try:
                        ent = await tg.get_entity(target_phone)
                        c_n = f"{getattr(ent, 'first_name', '') or ''} {getattr(ent, 'last_name', '') or ''}".strip() or "Client"
                        c_i = str(ent.id)
                    except: pass
                    f_url = await save_tg_media(event)
                    sent = await (tg.send_file(target_phone, os.path.join(FILES_DIR, f_url.split('/')[-1]), caption=message_text) if f_url else tg.send_message(target_phone, message_text))
                    await log_to_db(source="Manager", phone=target_phone, text=message_text, c_name=c_n, c_id=c_i, manager=s_phone, s_number=s_phone, f_url=f_url, direction="out", tg_id=sent.id)
                    await event.reply("✅ Отправлено")
                except Exception as e: await event.reply(f"❌ Ошибка: {str(e)}")
        
        # 2. ВХОДЯЩЕЕ ОТ КЛИЕНТА
        else:
            f_url = await save_tg_media(event)
            await log_to_db(source="Client", phone=s_phone or "Unknown", text=raw_text or "[Файл]", c_name=s_full_name, c_id=s_id, f_url=f_url, direction="in", tg_id=event.message.id)

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+').strip()
    text, mgr = data.get("text", ""), data.get("manager")
    tg = await get_client()
    try:
        c_n, c_i = "Client", phone
        try:
            ent = await tg.get_entity(phone)
            c_n = f"{getattr(ent, 'first_name', '') or ''} {getattr(ent, 'last_name', '') or ''}".strip() or "Client"
            c_i = str(ent.id)
        except: pass
        sent = await tg.send_message(phone, text)
        await log_to_db(source="1C", phone=phone, text=text, c_name=c_n, c_id=c_i, manager=mgr, s_number=None, direction="out", tg_id=sent.id)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+').strip()
    f_url, text, mgr = data.get("file"), data.get("text", ""), data.get("manager")
    tg = await get_client()
    try:
        c_n, c_i = "Client", phone
        try:
            ent = await tg.get_entity(phone)
            c_n = f"{getattr(ent, 'first_name', '') or ''} {getattr(ent, 'last_name', '') or ''}".strip() or "Client"
            c_i = str(ent.id)
        except: pass
        sent = await tg.send_file(phone, f_url, caption=text)
        await log_to_db(source="1C", phone=phone, text=text, c_name=c_n, c_id=c_i, manager=mgr, s_number=None, f_url=f_url, direction="out", tg_id=sent.id)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as c:
            rows = [dict(r) for r in await c.fetchall()]
        if rows:
            ids = [r['id'] for r in rows]
            await db.execute(f"UPDATE outbound_logs SET status='ok' WHERE id IN ({','.join(['?']*len(ids))})", ids)
            await db.commit()
        return jsonify(rows)

@app.route('/get_file/<filename>')
async def get_file(filename): 
    return await send_from_directory(FILES_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
