import os, asyncio, aiosqlite, re, uuid
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
                client_name TEXT, phone TEXT, manager_ref TEXT
            )
        """)
        await db.commit()
        print("✅ База данных инициализирована")

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager_fio=None, s_number=None, f_url=None, direction='in', tg_id=None):
    created_at = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(source), str(phone or ""), str(c_name or ""), str(c_id or ""), str(manager_fio or ""), str(s_number or ""), 'tg', str(text or ""), f_url, 'pending', direction, tg_id, created_at))
            await db.commit()
    except Exception as e: print(f"⚠️ DB Error: {e}")

async def get_topic_info(c_id_or_topic_id, by_topic=False):
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        query = "SELECT * FROM client_topics WHERE topic_id = ?" if by_topic else "SELECT * FROM client_topics WHERE client_id = ?"
        async with db.execute(query, (str(c_id_or_topic_id),)) as cursor:
            res = await cursor.fetchone()
            return dict(res) if res else None

async def find_last_outbound_manager(c_id):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT manager, source, created_at FROM outbound_logs 
                WHERE client_id = ? AND direction = 'out' AND manager != '' 
                ORDER BY created_at DESC LIMIT 1
            """, (str(c_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    print(f"   [DEBUG_HISTORY] Найдено в истории: Менеджер='{row['manager']}', Источник='{row['source']}', Дата='{row['created_at']}'")
                    return row['manager']
                print(f"   [DEBUG_HISTORY] В истории НИЧЕГО не найдено для клиента {c_id}")
                return ""
    except Exception as e:
        print(f"   [DEBUG_HISTORY] Ошибка поиска: {e}")
        return ""

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

    @tg.on(events.ChatAction)
    async def action_handler(event):
        if event.action_message and isinstance(event.action_message.action, types.MessageActionTopicDelete):
            t_id = event.action_message.reply_to.reply_to_msg_id
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM client_topics WHERE topic_id = ?", (t_id,))
                await db.commit()
            print(f"🗑️ Тема {t_id} удалена из базы")

    @tg.on(events.NewMessage())
    async def handler(event):
        if event.out: return # Игнорируем свои же сообщения

        sender = await event.get_sender()
        s_id = str(event.sender_id)
        raw_text = (event.raw_text or "").strip()

        # --- ЛОГИКА ДЛЯ ВХОДЯЩИХ В ЛИЧКУ ---
        if event.is_private:
            print(f"\n--- [DEBUG] Входящее от {s_id} ---")
            
            row = await get_topic_info(s_id)
            if row:
                print(f"   [DEBUG] Шаг 1: Найдена активная тема. Менеджер из темы: {row['manager_ref']}")
                msg_source = "Manager"
                m_fio = MANAGERS.get(row['manager_ref'], row['manager_ref'])
                m_phone = row['manager_ref']
            else:
                print(f"   [DEBUG] Шаг 1: Тема в базе не найдена. Переходим к истории логов.")
                msg_source = "1C"
                m_fio = await find_last_outbound_manager(s_id)
                m_phone = ""
            
            print(f"   [DEBUG] Итог: Source={msg_source}, Manager={m_fio}")
            
            f_url = await save_tg_media(event)
            await log_to_db(
                source=msg_source, phone=getattr(sender, 'phone', ''), text=raw_text, 
                c_name=f"{getattr(sender, 'first_name', '')} {getattr(sender, 'last_name', '')}",
                c_id=s_id, manager_fio=m_fio, s_number=m_phone, direction="in", tg_id=event.id
            )
            
            if row:
                try: await tg.send_message(GROUP_ID, f"💬 {raw_text}", reply_to=row['topic_id'])
                except: pass

        # --- ЛОГИКА ДЛЯ ГРУППЫ ---
        elif event.is_group:
            s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
            if s_phone in MANAGERS:
                if raw_text.startswith('#'):
                    # (Создание темы...)
                    match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
                    if not match: return
                    t_phone, c_name_input = match.group(1).strip(), match.group(2).strip()
                    try:
                        ent = await tg.get_entity(t_phone)
                        res = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=f"{c_name_input} {t_phone}"))
                        topic_id = next((u.id for u in res.updates if hasattr(u, 'id')), None)
                        if topic_id:
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name, phone, manager_ref) VALUES (?, ?, ?, ?, ?)",
                                               (str(ent.id), topic_id, c_name_input, t_phone, s_phone))
                                await db.commit()
                            await event.reply(f"✅ Тема создана.")
                    except Exception as e: await event.reply(f"❌ Ошибка: {str(e)}")
                
                elif event.reply_to_msg_id:
                    row = await get_topic_info(event.reply_to_msg_id, by_topic=True)
                    if row:
                        target_id = int(row['client_id'])
                        f_url = await save_tg_media(event)
                        if event.message.media: sent = await tg.send_file(target_id, event.message.media, caption=raw_text)
                        else: sent = await tg.send_message(target_id, raw_text)
                        await log_to_db(source="Manager", phone=row['phone'], c_name=row['client_name'], text=raw_text, c_id=str(target_id), manager_fio=MANAGERS[s_phone], s_number=s_phone, f_url=f_url, direction="out", tg_id=sent.id)

# --- API ---
@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone, text, mgr_fio = str(data.get("phone", "")).lstrip('+').strip(), data.get("text", ""), str(data.get("manager", ""))
    print(f"\n--- [DEBUG API] Запрос из 1С: Тел={phone}, Менеджер={mgr_fio} ---")
    tg = await get_client()
    try:
        ent = await tg.get_entity(phone)
        sent = await tg.send_message(ent.id, text)
        await log_to_db(source="1C", phone=phone, c_name=f"{ent.first_name or ''}", text=text, c_id=str(ent.id), manager_fio=mgr_fio, direction="out", tg_id=sent.id)
        print(f"   [DEBUG API] Успешно отправлено и сохранено для ID {ent.id}")
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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
