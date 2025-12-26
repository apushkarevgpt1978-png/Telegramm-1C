import os, asyncio, aiosqlite, re, uuid, httpx
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events, functions, types

app = Quart(__name__)

# --- CONFIG ---
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
FILES_DIR = '/app/files'
BASE_URL = os.environ.get('BASE_URL', 'http://192.168.121.99:5000')
GROUP_ID = -1003599844429

# Green-API Config
WA_ID_INSTANCE = os.environ.get('WA_ID_INSTANCE', '')
WA_API_TOKEN = os.environ.get('WA_API_TOKEN', '')
WA_API_URL = f"https://api.green-api.com/waInstance{WA_ID_INSTANCE}"

mgr_raw = os.environ.get('MANAGERS_PHONES', '')
MANAGERS = {ph.strip().lstrip('+'): name.strip() for item in mgr_raw.split(',') if ':' in item for ph, name in [item.split(':', 1)]}

if not os.path.exists(FILES_DIR): os.makedirs(FILES_DIR)

client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.start()
    return client

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, client_name TEXT, client_id TEXT,
                sender_number TEXT, messenger TEXT, message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER,
                direction TEXT, error_text TEXT, created_at DATETIME, manager TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS client_topics (
                client_id TEXT PRIMARY KEY, topic_id INTEGER,
                client_name TEXT, phone TEXT, manager_ref TEXT, messenger TEXT DEFAULT 'tg'
            )
        """)
        try: await db.execute("ALTER TABLE client_topics ADD COLUMN messenger TEXT DEFAULT 'tg'")
        except: pass
        await db.commit()

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager_fio=None, s_number=None, f_url=None, direction='in', tg_id=None, messenger='tg'):
    # ЗАЩИТА 1С: Убираем NULL (None), заменяя на пустые строки или дефолты
    source = str(source or "Система")
    phone = str(phone or "")
    c_name = str(c_name or phone or "Клиент")
    c_id = str(c_id or phone or "")
    manager_fio = str(manager_fio or "Система")
    s_number = str(s_number or "")
    text = str(text or "")
    f_url = str(f_url or "")
    messenger = str(messenger or "tg")
    
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, phone, c_name, c_id, manager_fio, s_number, messenger, text, f_url, 'pending', direction, tg_id, datetime.now()))
            await db.commit()
    except Exception as e: print(f"⚠️ DB Error: {e}")

async def get_topic_info_with_retry(c_id_or_topic_id, by_topic=False):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM client_topics WHERE topic_id = ?" if by_topic else "SELECT * FROM client_topics WHERE client_id = ?"
        async with db.execute(query, (str(c_id_or_topic_id),)) as cursor:
            row = await cursor.fetchone()
            if not row: return None
            try:
                tg = await get_client()
                res = await tg.get_messages(GROUP_ID, ids=int(row['topic_id']))
                if res and not isinstance(res, types.MessageEmpty): return dict(row)
                raise ValueError()
            except:
                await db.execute("DELETE FROM client_topics WHERE client_id = ?", (str(row['client_id']),))
                await db.commit()
                return None

# --- WHATSAPP HELPERS ---
async def download_wa_file(file_id, file_name):
    async with httpx.AsyncClient() as wa_c:
        url = f"{WA_API_URL}/downloadFile/{WA_API_TOKEN}"
        resp = await wa_c.post(url, json={"fileId": file_id}, timeout=30)
        if resp.status_code == 200:
            ext = os.path.splitext(file_name)[1] or ".bin"
            local_name = f"{uuid.uuid4()}{ext}"
            path = os.path.join(FILES_DIR, local_name)
            with open(path, "wb") as f: f.write(resp.content)
            return f"{BASE_URL}/get_file/{local_name}"
    return ""

async def wa_listener():
    print("🚀 Поллинг WhatsApp запущен...")
    async with httpx.AsyncClient() as wa_c:
        while True:
            try:
                resp = await wa_c.get(f"{WA_API_URL}/receiveNotification/{WA_API_TOKEN}", timeout=20)
                if resp.status_code == 200 and resp.json():
                    data = resp.json()
                    receipt_id = data.get('receiptId')
                    body = data.get('body', {})
                    
                    if body.get('typeWebhook') == 'incomingMessageReceived':
                        s_data = body.get('senderData', {})
                        phone = s_data.get('chatId', '').split('@')[0]
                        name = s_data.get('senderName', 'WA Client')
                        m_data = body.get('messageData', {})
                        m_type = m_data.get('typeMessage', '')
                        
                        text, f_url = "", ""
                        if m_type == 'textMessage': 
                            text = m_data.get('textMessageData', {}).get('text', '')
                        elif m_type == 'extendedTextMessage': 
                            text = m_data.get('extendedTextMessageData', {}).get('text', '')
                        elif 'fileMessageData' in m_data:
                            fd = m_data['fileMessageData']
                            text = fd.get('caption', '[Файл]')
                            f_url = await download_wa_file(fd.get('fileId'), fd.get('fileName', 'file'))
                        
                        await handle_wa_incoming(phone, name, text or "[Медиа]", f_url)

                    await wa_c.delete(f"{WA_API_URL}/deleteNotification/{WA_API_TOKEN}/{receipt_id}")
                await asyncio.sleep(1)
            except: await asyncio.sleep(5)

async def handle_wa_incoming(phone, name, text, f_url):
    tg = await get_client()
    row = await get_topic_info_with_retry(phone)
    if not row:
        try:
            # Название темы БЕЗ скобок
            res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"WA {name} {phone}"))
            topic_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
            if topic_id:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref, messenger) VALUES (?, ?, ?, ?, ?, ?)",
                                   (phone, topic_id, name, phone, "system", "wa"))
                    await db.commit()
                row = {'topic_id': topic_id, 'client_name': name, 'phone': phone}
        except: return

    display_text = f"🟢 WhatsApp | {name}:\n{text}"
    if f_url: display_text += f"\n📎 {f_url}"
    await tg.send_message(GROUP_ID, display_text, reply_to=row['topic_id'])
    await log_to_db(source="Manager", phone=phone, text=text, c_name=name, c_id=phone, f_url=f_url, direction="in", messenger="wa")

# --- TELEGRAM HANDLERS ---
async def start_listener():
    tg = await get_client()

    @tg.on(events.ChatAction)
    async def action_handler(event):
        if event.action_message and event.action_message.action:
            if isinstance(event.action_message.action, types.MessageActionForumTopicDelete):
                t_id = event.action_message.reply_to.reply_to_msg_id if event.action_message.reply_to else None
                if t_id:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("DELETE FROM client_topics WHERE topic_id = ?", (t_id,))
                        await db.commit()

    @tg.on(events.NewMessage())
    async def handler(event):
        if event.out and not event.is_group: return
        sender = await event.get_sender()
        s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()

        if event.is_group and event.reply_to_msg_id:
            row = await get_topic_info_with_retry(event.reply_to_msg_id, by_topic=True)
            if row:
                text = (event.raw_text or "").strip()
                # Сохраняем медиа если оно есть
                f_url = ""
                if event.message.media:
                    ext = ".jpg" # дефолт
                    if hasattr(event.message.media, 'document'):
                        for attr in event.message.media.document.attributes:
                            if hasattr(attr, 'file_name'): ext = os.path.splitext(attr.file_name)[1]
                    fname = f"{uuid.uuid4()}{ext}"
                    await event.message.download_media(file=os.path.join(FILES_DIR, fname))
                    f_url = f"{BASE_URL}/get_file/{fname}"

                if row.get('messenger') == 'wa':
                    async with httpx.AsyncClient() as wa_c:
                        if f_url: # ОТПРАВКА В WA ЧЕРЕЗ URL
                            await wa_c.post(f"{WA_API_URL}/sendFileByUrl/{WA_API_TOKEN}", json={
                                "chatId": f"{row['phone']}@c.us", "urlFile": f_url, "fileName": "file", "caption": text
                            })
                        else:
                            await wa_c.post(f"{WA_API_URL}/sendMessage/{WA_API_TOKEN}", json={
                                "chatId": f"{row['phone']}@c.us", "message": text
                            })
                    await log_to_db(source="Manager", phone=row['phone'], text=text, c_name=row['client_name'], c_id=row['client_id'], manager_fio=MANAGERS.get(s_phone, ""), f_url=f_url, direction="out", messenger="wa")
                else:
                    # TG
                    ent = await tg.get_entity(int(row['client_id']))
                    if event.message.media: sent = await tg.send_file(ent, event.message.media, caption=text)
                    else: sent = await tg.send_message(ent, text)
                    await log_to_db(source="Manager", phone=row['phone'], text=text, c_name=row['client_name'], c_id=row['client_id'], manager_fio=MANAGERS.get(s_phone, ""), f_url=f_url, direction="out", tg_id=sent.id, messenger="tg")
            return

        if event.is_group and event.raw_text.startswith('#'):
            match = re.search(r'#(\d+)/(.*)', event.raw_text, re.DOTALL)
            if match:
                t_phone, c_name = match.group(1).strip(), match.group(2).strip()
                try:
                    ent = await tg.get_entity(t_phone)
                    res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"{c_name} {t_phone}"))
                    t_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
                    if t_id:
                        async with aiosqlite.connect(DB_PATH) as db:
                            await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref, messenger) VALUES (?, ?, ?, ?, ?, ?)",
                                           (str(ent.id), t_id, c_name, t_phone, s_phone, "tg"))
                            await db.commit()
                        await event.reply(f"✅ ТГ Тема создана")
                except Exception as e: await event.reply(f"❌ {e}")

# --- API ---
@app.route('/send', methods=['POST'])
async def api_send():
    data = await request.get_json()
    phone, text, mgr, messenger = str(data.get("phone", "")).lstrip('+').strip(), data.get("text", ""), data.get("manager", ""), data.get("messenger", "tg")
    try:
        if messenger == "wa":
            async with httpx.AsyncClient() as wa_c:
                await wa_c.post(f"{WA_API_URL}/sendMessage/{WA_API_TOKEN}", json={"chatId": f"{phone}@c.us", "message": text})
            await log_to_db(source="1C", phone=phone, text=text, manager_fio=mgr, direction="out", messenger="wa")
        else:
            tg = await get_client(); ent = await tg.get_entity(phone)
            sent = await tg.send_message(ent, text)
            await log_to_db(source="1C", phone=phone, text=text, c_id=str(ent.id), manager_fio=mgr, direction="out", tg_id=sent.id, messenger="tg")
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as c:
            rows = [dict(r) for r in await c.fetchall()]
        if rows:
            await db.execute(f"UPDATE outbound_logs SET status='ok' WHERE id IN ({','.join(['?']*len(rows))})", [r['id'] for r in rows])
            await db.commit()
        return jsonify(rows)

@app.route('/get_file/<filename>')
async def get_file(filename): return await send_from_directory(FILES_DIR, filename)

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())
    asyncio.create_task(wa_listener())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
