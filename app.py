import os, asyncio, aiosqlite, re, uuid
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events, functions, types

app = Quart(__name__)

# --- НАСТРОЙКИ ---
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
MANAGERS = os.environ.get('MANAGERS_PHONES', '').split(',')
FILES_DIR = '/app/files'
BASE_URL = os.environ.get('BASE_URL', 'http://192.168.121.99:5000')
GROUP_ID = -1003599844429

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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS client_topics (
                client_id TEXT PRIMARY KEY,
                topic_id INTEGER,
                client_name TEXT,
                phone TEXT,
                manager_ref TEXT
            )
        """)
        # Попытка добавить колонку если база старая
        try: await db.execute("ALTER TABLE client_topics ADD COLUMN manager_ref TEXT")
        except: pass
        await db.commit()

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager=None, s_number=None, f_url=None, direction='in', tg_id=None):
    messenger = 'tg'
    created_at = datetime.now()
    # Чистим от None
    s_phone = str(phone) if phone else ""
    s_manager = str(manager) if manager else ""
    s_cid = str(c_id) if c_id else ""
    
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, s_phone, c_name, s_cid, s_manager, s_number, messenger, text, f_url, 'pending', direction, tg_id, created_at))
            await db.commit()
    except Exception as e:
        print(f"⚠️ ОШИБКА БД: {e}")

async def get_topic_info(c_id_or_topic_id, by_topic=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM client_topics WHERE topic_id = ?" if by_topic else "SELECT * FROM client_topics WHERE client_id = ?"
        async with db.execute(query, (str(c_id_or_topic_id),)) as cursor:
            return await cursor.fetchone()

async def delete_broken_topic(topic_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM client_topics WHERE topic_id = ?", (topic_id,))
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
        return f"{BASE_URL}/get_file/{filename}"
    return None

async def start_listener():
    tg = await get_client()
    managers_list = [m.strip() for m in MANAGERS if m.strip()]
    
    @tg.on(events.NewMessage())
    async def handler(event):
        sender = await event.get_sender()
        s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
        s_id = str(event.sender_id)
        raw_text = (event.raw_text or "").strip()

        # 1. ЛОГИКА МЕНЕДЖЕРА
        if s_phone in managers_list:
            if raw_text.startswith('#'):
                match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
                if not match: return
                t_phone, content = match.group(1).strip(), match.group(2).strip()
                try:
                    ent = await tg.get_entity(t_phone)
                    c_id = str(ent.id)
                    row = await get_topic_info(c_id)
                    topic_id = row['topic_id'] if row else None
                    
                    if not topic_id:
                        res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"{t_phone} {content[:15]}"))
                        topic_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
                        if topic_id:
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref) VALUES (?, ?, ?, ?, ?)",
                                               (c_id, topic_id, content[:50], t_phone, s_phone))
                                await db.commit()

                    f_url = await save_tg_media(event)
                    sent = await (tg.send_file(ent.id, os.path.join(FILES_DIR, f_url.split('/')[-1]), caption=content) if f_url else tg.send_message(ent.id, content))
                    await log_to_db(source="Manager", phone=t_phone, text=content, c_id=c_id, manager=s_phone, f_url=f_url, direction="out", tg_id=sent.id)
                    
                    try: await tg.send_message(GROUP_ID, f"📤 Ответ: {content}", reply_to=topic_id)
                    except: pass
                    await event.reply(f"✅ Ушло")
                except Exception as e: await event.reply(f"❌ Ошибка: {str(e)}")
                return

            if event.is_group and event.reply_to:
                row = await get_topic_info(event.reply_to_msg_id, by_topic=True)
                if row:
                    target_id = int(row['client_id'])
                    sent = await tg.send_message(target_id, raw_text)
                    await log_to_db(source="Manager", phone=row['phone'], text=raw_text, c_id=str(target_id), manager=s_phone, direction="out", tg_id=sent.id)

        # 2. ЛОГИКА КЛИЕНТА
        elif event.is_private:
            f_url = await save_tg_media(event)
            s_full_name = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip() or "Client"
            
            row = await get_topic_info(s_id)
            # Берем телефон менеджера, который создал эту тему
            m_phone = row['manager_ref'] if row else ""
            
            await log_to_db(source="Client", phone=s_phone, text=raw_text or "[Медиа]", c_name=s_full_name, c_id=s_id, manager=m_phone, f_url=f_url, direction="in", tg_id=event.message.id)
            
            if row:
                try:
                    if event.message.media:
                        await tg.send_file(GROUP_ID, event.message.media, caption=f"📎 Файл: {raw_text or ''}", reply_to=row['topic_id'])
                    else:
                        await tg.send_message(GROUP_ID, f"💬 {raw_text}", reply_to=row['topic_id'])
                except Exception as e:
                    if "reply_to_msg_id_invalid" in str(e).lower(): await delete_broken_topic(row['topic_id'])

    @tg.on(events.ChatAction)
    async def action_handler(event):
        if event.is_group and event.action_message and isinstance(event.action_message.action, types.MessageActionTopicDelete):
            await delete_broken_topic(event.action_message.reply_to_msg_id)

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

# --- API Роуты ---
@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone, text, mgr = str(data.get("phone", "")).lstrip('+').strip(), data.get("text", ""), data.get("manager")
    tg = await get_client()
    try:
        ent = await tg.get_entity(phone)
        sent = await tg.send_message(ent.id, text)
        await log_to_db(source="1C", phone=phone, text=text, c_id=str(ent.id), manager=mgr, direction="out", tg_id=sent.id)
        row = await get_topic_info(ent.id)
        if row:
            try: await tg.send_message(GROUP_ID, f"🤖 1C: {text}", reply_to=row['topic_id'])
            except: await delete_broken_topic(row['topic_id'])
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone, f_url, text, mgr = str(data.get("phone", "")).lstrip('+').strip(), data.get("file"), data.get("text", ""), data.get("manager")
    tg = await get_client()
    try:
        ent = await tg.get_entity(phone)
        sent = await tg.send_file(ent.id, f_url, caption=text)
        await log_to_db(source="1C", phone=phone, text=text, c_id=str(ent.id), manager=mgr, f_url=f_url, direction="out", tg_id=sent.id)
        row = await get_topic_info(ent.id)
        if row:
            try: await tg.send_message(GROUP_ID, f"🤖 1C Файл: {text}", reply_to=row['topic_id'])
            except: await delete_broken_topic(row['topic_id'])
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
async def get_file(filename): return await send_from_directory(FILES_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
