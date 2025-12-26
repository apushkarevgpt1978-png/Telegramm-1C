import os, asyncio, aiosqlite, re, uuid, httpx
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events, functions, types, errors

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
MANAGERS = {}
if mgr_raw:
    for item in mgr_raw.split(','):
        if ':' in item:
            ph, name = item.split(':', 1)
            MANAGERS[ph.strip().lstrip('+')] = name.strip()

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
                sender_number TEXT, messenger TEXT DEFAULT 'tg', message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER,
                direction TEXT, error_text TEXT, created_at DATETIME, manager TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS client_topics (
                client_id TEXT PRIMARY KEY, topic_id INTEGER,
                client_name TEXT, phone TEXT, manager_ref TEXT,
                messenger TEXT DEFAULT 'tg'
            )
        """)
        try: await db.execute("ALTER TABLE client_topics ADD COLUMN messenger TEXT DEFAULT 'tg'")
        except: pass
        await db.commit()
        print("✅ База данных готова (TG + WA)")

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager_fio=None, s_number=None, f_url=None, direction='in', tg_id=None, messenger='tg'):
    created_at = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(source), str(phone or ""), str(c_name or ""), str(c_id or ""), str(manager_fio or ""), str(s_number or ""), messenger, str(text or ""), f_url, 'pending', direction, tg_id, created_at))
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
                else: raise ValueError()
            except:
                await db.execute("DELETE FROM client_topics WHERE client_id = ?", (str(row['client_id']),))
                await db.commit()
                return None

# --- WHATSAPP LISTENER (GREEN-API) ---
async def wa_listener():
    print("🚀 Слушатель WhatsApp (Polling) запущен")
    async with httpx.AsyncClient() as wa_client:
        while True:
            try:
                receive_url = f"{WA_API_URL}/receiveNotification/{WA_API_TOKEN}"
                resp = await wa_client.get(receive_url, timeout=20)
                if resp.status_code == 200 and resp.json():
                    data = resp.json()
                    receipt_id = data.get('receiptId')
                    body = data.get('body', {})
                    
                    if body.get('typeWebhook') == 'incomingMessageReceived':
                        sender_data = body.get('senderData', {})
                        wa_phone = sender_data.get('chatId', '').split('@')[0]
                        wa_name = sender_data.get('senderName', 'WA Client')
                        msg_data = body.get('messageData', {})
                        text = msg_data.get('textMessageData', {}).get('text') or msg_data.get('extendedTextMessageData', {}).get('text') or "[Файл/Медиа]"
                        
                        await handle_wa_incoming(wa_phone, wa_name, text)

                    # Удаляем уведомление, чтобы очередь двигалась
                    await wa_client.delete(f"{WA_API_URL}/deleteNotification/{WA_API_TOKEN}/{receipt_id}")
                await asyncio.sleep(1)
            except Exception as e:
                await asyncio.sleep(5)

async def handle_wa_incoming(phone, name, text):
    tg = await get_client()
    # Ищем клиента в базе (по номеру или id)
    # Для WA в качестве client_id используем номер
    row = await get_topic_info_with_retry(phone) 
    
    if not row:
        # Создаем тему [WA]
        try:
            res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"[WA] {name} {phone}"))
            topic_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
            if topic_id:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref, messenger) VALUES (?, ?, ?, ?, ?, ?)",
                                   (phone, topic_id, name, phone, "system", "wa"))
                    await db.commit()
                row = {'topic_id': topic_id, 'client_name': name, 'phone': phone, 'messenger': 'wa'}
        except Exception as e: print(f"❌ Ошибка создания темы WA: {e}"); return

    # Логируем и шлем в ТГ
    await log_to_db(source="Manager", phone=phone, text=text, c_name=name, c_id=phone, manager_fio="", direction="in", messenger="wa")
    await tg.send_message(GROUP_ID, f"🟢 *WhatsApp*: {text}", reply_to=row['topic_id'])

# --- TELEGRAM HANDLER ---
async def start_listener():
    tg = await get_client()

    @tg.on(events.ChatAction)
    async def action_handler(event):
        if event.action_message and event.action_message.action:
            # ИСПРАВЛЕНО: Правильный тип для удаления темы
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
                # Если тема WhatsApp
                if row.get('messenger') == 'wa':
                    async with httpx.AsyncClient() as wa_c:
                        url = f"{WA_API_URL}/sendMessage/{WA_API_TOKEN}"
                        payload = {"chatId": f"{row['phone']}@c.us", "message": text}
                        await wa_c.post(url, json=payload)
                    await log_to_db(source="Manager", phone=row['phone'], text=text, c_name=row['client_name'], c_id=row['client_id'], manager_fio=MANAGERS.get(s_phone, ""), direction="out", messenger="wa")
                else:
                    # Обычный Telegram
                    target_ent = await tg.get_entity(int(row['client_id']))
                    sent = await tg.send_message(target_ent, text)
                    await log_to_db(source="Manager", phone=row['phone'], text=text, c_name=row['client_name'], c_id=row['client_id'], manager_fio=MANAGERS.get(s_phone, ""), direction="out", tg_id=sent.id, messenger="tg")
            return
        
        # Обработка команд #номер/имя (создание TG тем)
        if event.is_group and event.raw_text.startswith('#'):
            match = re.search(r'#(\d+)/(.*)', event.raw_text, re.DOTALL)
            if not match: 
                await event.reply("❌ Пример: `#79876543210/Иванов Иван`")
                return
            t_phone, c_name = match.group(1).strip(), match.group(2).strip()
            try:
                ent = await tg.get_entity(t_phone)
                res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"{c_name} {t_phone}"))
                topic_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
                if topic_id:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref, messenger) VALUES (?, ?, ?, ?, ?, ?)",
                                       (str(ent.id), topic_id, c_name, t_phone, s_phone, "tg"))
                        await db.commit()
                    await event.reply(f"✅ Тема создана для {t_phone}")
            except Exception as e: await event.reply(f"❌ {e}")

# --- API ---
@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone, text, mgr_fio = str(data.get("phone", "")).lstrip('+').strip(), data.get("text", ""), str(data.get("manager", ""))
    # 1С шлет в WhatsApp, если указано? Давай по умолчанию пока TG, либо добавь messenger в JSON
    messenger = data.get("messenger", "tg")
    try:
        if messenger == "wa":
            async with httpx.AsyncClient() as wa_c:
                await wa_c.post(f"{WA_API_URL}/sendMessage/{WA_API_TOKEN}", json={"chatId": f"{phone}@c.us", "message": text})
            await log_to_db(source="1C", phone=phone, text=text, manager_fio=mgr_fio, direction="out", messenger="wa")
        else:
            tg = await get_client(); ent = await tg.get_entity(phone)
            sent = await tg.send_message(ent, text)
            await log_to_db(source="1C", phone=phone, text=text, c_id=str(ent.id), manager_fio=mgr_fio, direction="out", tg_id=sent.id, messenger="tg")
        return jsonify({"status": "ok"}), 200
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

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())
    asyncio.create_task(wa_listener()) # Запуск WhatsApp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
