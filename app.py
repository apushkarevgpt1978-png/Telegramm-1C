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

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, client_name TEXT, client_id TEXT,
                sender_number TEXT, messenger TEXT, message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id TEXT,
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
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(source or "Система"), str(phone or ""), str(c_name or phone or "Клиент"), str(c_id or ""), str(manager_fio or "Система"), str(s_number or ""), str(messenger or "tg"), str(text or ""), str(f_url or ""), 'pending', str(direction or "in"), str(tg_id or ""), datetime.now()))
            await db.commit()
    except Exception as e: print(f"⚠️ DB Error: {e}")

async def get_topic_info_with_retry(search_val, by_topic=False):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        q = "SELECT * FROM client_topics WHERE topic_id = ?" if by_topic else "SELECT * FROM client_topics WHERE client_id = ? OR phone = ?"
        async with db.execute(q, (str(search_val),) if by_topic else (str(search_val), str(search_val))) as cursor:
            row = await cursor.fetchone()
            if not row: return None
            try:
                tg = await get_client()
                res = await tg.get_messages(GROUP_ID, ids=int(row['topic_id']))
                if res and not isinstance(res, types.MessageEmpty): return dict(row)
                await db.execute("DELETE FROM client_topics WHERE topic_id = ?", (row['topic_id'],))
                await db.commit()
                return None
            except: return dict(row)

async def download_wa_file(file_id, file_name):
    async with httpx.AsyncClient() as wa_c:
        resp = await wa_c.post(f"{WA_API_URL}/downloadFile/{WA_API_TOKEN}", json={"fileId": file_id}, timeout=30)
        if resp.status_code == 200:
            local_name = f"{uuid.uuid4()}{os.path.splitext(file_name)[1] or '.bin'}"
            with open(os.path.join(FILES_DIR, local_name), "wb") as f: f.write(resp.content)
            return f"{BASE_URL}/get_file/{local_name}"
    return ""

async def wa_listener():
    print("🚀 СЛУШАТЕЛЬ WA ЗАПУЩЕН")
    async with httpx.AsyncClient() as wa_c:
        while True:
            try:
                resp = await wa_c.get(f"{WA_API_URL}/receiveNotification/{WA_API_TOKEN}", timeout=20)
                if resp.status_code == 200 and resp.json():
                    data = resp.json()
                    receipt_id = data.get('receiptId')
                    body = data.get('body', {})
                    
                    # ГЛОБАЛЬНЫЙ ПРИНТ ДЛЯ ПОРТЕЙНЕРА (ВИДИМ ВСЁ)
                    print(f"\n--- NEW NOTIFICATION: {body.get('typeWebhook')} ---")

                    if body.get('typeWebhook') == 'incomingMessageReceived':
                        id_message = body.get('idMessage')
                        chat_id = body.get('chatId', '')
                        sender_data = body.get('senderData', {})
                        s_id = sender_data.get('chatId', '')
                        s_name = sender_data.get('senderName', 'WA Client')
                        m_data = body.get('messageData', {})
                        m_type = m_data.get('typeMessage', '')
                        phone = chat_id.split('@')[0]
                        
                        text, f_url = "", ""
                        if m_type == 'textMessage': text = m_data.get('textMessageData', {}).get('text', '')
                        elif m_type == 'extendedTextMessage': text = m_data.get('extendedTextMessageData', {}).get('text', '')
                        elif m_type in ['imageMessage', 'documentMessage', 'videoMessage']:
                            f_info = m_data.get(m_type)
                            if f_info:
                                text = f_info.get('caption', '')
                                f_url = await download_wa_file(f_info.get('fileId'), f_info.get('fileName', 'file'))

                        if not text: text = "[Фото]" if m_type == 'imageMessage' else "[Файл]" if f_url else "[Пустое сообщение]"

                        # ПРИНТ ПОЛЕЙ ДЛЯ АНДРЕЯ
                        print(f"🆔 ID: {id_message} | 📞 Phone: {phone} | 💬 Text: {text}")

                        await handle_wa_incoming(phone, s_name, text, f_url, id_message, s_id)

                    await wa_c.delete(f"{WA_API_URL}/deleteNotification/{WA_API_TOKEN}/{receipt_id}")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"⚠️ Ошибка WA: {e}")
                await asyncio.sleep(5)

async def handle_wa_incoming(phone, name, text, f_url, id_msg, s_id):
    tg = await get_client()
    row = await get_topic_info_with_retry(phone)
    if not row:
        print(f"🆕 Создаю новую тему для {phone}")
        res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"WA {name} {phone}"))
        topic_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
        if topic_id:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref, messenger) VALUES (?, ?, ?, ?, ?, ?)", (phone, topic_id, name, phone, "system", "wa"))
                await db.commit()
            row = {'topic_id': topic_id}
    
    display = f"🟢 WhatsApp | {name}:\n{text}"
    if f_url: display += f"\n📎 {f_url}"
    await tg.send_message(GROUP_ID, display, reply_to=row['topic_id'])
    await log_to_db("Manager", phone, text, name, s_id, None, None, f_url, "in", id_msg, "wa")

async def start_listener():
    tg = await get_client()
    @tg.on(events.NewMessage())
    async def handler(event):
        if event.out and not event.is_group: return
        if event.is_group and event.reply_to_msg_id:
            row = await get_topic_info_with_retry(event.reply_to_msg_id, True)
            if row and row.get('messenger') == 'wa':
                text = (event.raw_text or "").strip()
                f_url = ""
                if event.message.media:
                    fname = f"{uuid.uuid4()}.jpg"
                    await event.message.download_media(file=os.path.join(FILES_DIR, fname))
                    f_url = f"{BASE_URL}/get_file/{fname}"
                async with httpx.AsyncClient() as wa_c:
                    p = {"chatId": f"{row['phone']}@c.us", "caption": text}
                    if f_url: p["urlFile"] = f_url; p["fileName"] = "file"; await wa_c.post(f"{WA_API_URL}/sendFileByUrl/{WA_API_TOKEN}", json=p)
                    else: p["message"] = text; await wa_c.post(f"{WA_API_URL}/sendMessage/{WA_API_TOKEN}", json=p)
                await log_to_db("Manager", row['phone'], text, row['client_name'], f"{row['phone']}@c.us", None, None, f_url, "out", None, "wa")

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending' AND direction = 'in'") as c:
            rows = await c.fetchall()
        res = [{"idMessage": r['tg_message_id'], "senderId": r['client_id'], "chatId": f"{r['phone']}@c.us" if r['messenger'] == 'wa' else r['phone'], "senderName": r['client_name'], "textMessage": r['message_text'], "downloadUrl": r['file_url'], "typeMessage": "imageMessage" if r['file_url'] else "textMessage"} for r in rows]
        if res:
            await db.execute(f"UPDATE outbound_logs SET status='ok' WHERE id IN ({','.join(['?']*len(rows))})", [r['id'] for r in rows])
            await db.commit()
        return jsonify(res)

@app.route('/get_file/<filename>')
async def get_file(filename): return await send_from_directory(FILES_DIR, filename)

@app.before_serving
async def startup():
    print("\n" + "!"*50)
    print("!!! ГЕНА ЗАПУЩЕН. ВЕРСИЯ С ЛОГАМИ И ПОИСКОМ ПО ТЕЛЕФОНУ !!!")
    print("!"*50 + "\n")
    await init_db()
    asyncio.create_task(start_listener())
    asyncio.create_task(wa_listener())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
