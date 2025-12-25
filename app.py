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

client = None

async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.start()
    return client

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
            # ВАЖНО: Смотрим что именно находит этот запрос
            async with db.execute("""
                SELECT manager, source FROM outbound_logs 
                WHERE client_id = ? AND direction = 'out' AND manager != '' 
                ORDER BY created_at DESC LIMIT 1
            """, (str(c_id),)) as cursor:
                row = await cursor.fetchone()
                if row:
                    print(f"   [DEBUG_HISTORY] Найдено: {row['manager']} (Source: {row['source']})")
                    return row['manager']
                print(f"   [DEBUG_HISTORY] В истории ничего не найдено для ID {c_id}")
                return ""
    except Exception as e:
        print(f"   [DEBUG_HISTORY] Ошибка БД: {e}")
        return ""

async def start_listener():
    tg = await get_client()

    @tg.on(events.NewMessage())
    async def handler(event):
        if event.out: return # Игнорируем всё, что Гена шлет сам

        sender = await event.get_sender()
        s_id = str(event.sender_id)
        
        # ЛОГИРОВАНИЕ ВХОДА
        if event.is_private:
            print(f"\n--- [NEW INCOMING] ID: {s_id} ---")
            
            # Шаг 1: Проверка темы
            row = await get_topic_info(s_id)
            if row:
                print(f"   [STEP 1] Найдена тема в базе: TopicID {row['topic_id']}, ManagerRef: {row['manager_ref']}")
                msg_source = "Manager"
                m_fio = MANAGERS.get(row['manager_ref'], row['manager_ref'] or "Manager")
                m_phone = row['manager_ref']
            else:
                print(f"   [STEP 1] Тема в базе НЕ найдена.")
                # Шаг 2: История
                msg_source = "1C"
                print(f"   [STEP 2] Ищу в истории для клиента {s_id}...")
                m_fio = await find_last_outbound_manager(s_id)
                m_phone = ""
            
            # ИТОГОВЫЙ ВЫБОР
            print(f"   [FINAL DECISION] Source: {msg_source}, Manager: {m_fio}")
            
            await log_to_db(
                source=msg_source, phone=getattr(sender, 'phone', ''), 
                text=event.raw_text, c_name=f"{getattr(sender, 'first_name', '')} {getattr(sender, 'last_name', '')}",
                c_id=s_id, manager_fio=m_fio, s_number=m_phone, direction="in", tg_id=event.id
            )
            
            if row:
                try:
                    await tg.send_message(GROUP_ID, f"💬 {event.raw_text}", reply_to=row['topic_id'])
                except: pass

@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone, text, mgr_fio = str(data.get("phone", "")).lstrip('+').strip(), data.get("text", ""), str(data.get("manager", ""))
    print(f"\n--- [API SEND] Phone: {phone}, Manager: {mgr_fio} ---")
    tg = await get_client()
    try:
        ent = await tg.get_entity(phone)
        sent = await tg.send_message(ent.id, text)
        # Логируем отправку от 1С
        await log_to_db(source="1C", phone=phone, c_name=f"{ent.first_name or ''}", text=text, c_id=str(ent.id), manager_fio=mgr_fio, direction="out", tg_id=sent.id)
        print(f"   [API SEND] Записано в базу для ID {ent.id}")
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

# ... (остальные роуты без изменений) ...

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
