import os, asyncio, aiosqlite, re, uuid
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events

app = Quart(__name__)

# --- НАСТРОЙКИ (Переменные окружения) ---
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')
SESSION_PATH = os.environ.get('TG_SESSION_PATH', '/app/data/GenaAPI')
DB_PATH = os.environ.get('DB_PATH', '/app/data/gateway_messages.db')
MANAGERS = os.environ.get('MANAGERS_PHONES', '').split(',')
FILES_DIR = '/app/files'
BASE_URL = os.environ.get('BASE_URL', 'http://192.168.121.99:5000/get_file')

if not os.path.exists(FILES_DIR): os.makedirs(FILES_DIR)

client = None
async def get_client():
    global client
    if client is None:
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
        await client.start()
        print("--- ГЕНА УСПЕШНО ЗАПУЩЕН И АВТОРИЗОВАН ---")
    return client

async def init_db():
    print(f"--- Инициализация БД: {DB_PATH} ---")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, client_name TEXT, client_id TEXT,
                sender_number TEXT, messenger TEXT DEFAULT 'tg', message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER,
                direction TEXT, error_text TEXT, created_at DATETIME
            )
        """)
        await db.commit()

async def log_to_db(source, phone, text, sender=None, f_url=None, c_id=None, c_name=None, status='pending', direction='out', tg_id=None, error=None):
    messenger = 'tg'
    created_at = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, error_text, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, phone, c_name, c_id, sender, messenger, text, f_url, status, direction, tg_id, error, created_at))
            await db.commit()
            print(f"✅ ЗАПИСЬ В БД: {c_name} (ID: {c_id}) | Статус: {status} | Dir: {direction}")
    except Exception as e:
        print(f"⚠️ ОШИБКА ЗАПИСИ В БД: {e}")

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

# --- СЛУШАТЕЛЬ ТЕЛЕГРАМ ---
async def start_listener():
    tg = await get_client()
    managers_list = [m.strip() for m in MANAGERS if m.strip()]
    
    @tg.on(events.NewMessage(incoming=True))
    async def handler(event):
        if not event.is_private: return
        sender = await event.get_sender()
        s_phone = str(getattr(sender, 'phone', '')).lstrip('+').strip()
        
        # ЛОГИКА МЕНЕДЖЕРА (#номер/текст)
        if s_phone in managers_list:
            msg_content = (event.raw_text or "").strip()
            match = re.search(r'#(\d+)/(.*)', msg_content, re.DOTALL)
            
            if match:
                target, msg = match.group(1).strip(), match.group(2).strip()
                real_name, real_id = "Client", target
                try:
                    entity = await tg.get_entity(target)
                    fn, ln = getattr(entity, 'first_name', '') or "", getattr(entity, 'last_name', '') or ""
                    real_name = f"{fn} {ln}".strip() or "Client"
                    real_id = str(getattr(entity, 'id', target))
                except: pass

                try:
                    f_url = await save_tg_media(event)
                    if f_url:
                        local_path = os.path.join(FILES_DIR, f_url.split('/')[-1])
                        sent = await tg.send_file(target, local_path, caption=msg)
                    else:
                        sent = await tg.send_message(target, msg)
                    
                    await log_to_db("Manager", target, msg, sender=s_phone, f_url=f_url, c_id=real_id, c_name=real_name, direction="out", tg_id=sent.id)
                    await event.reply(f"✅ Отправлено: {real_name}")
                except Exception as e: 
                    await event.reply(f"❌ Ошибка: {str(e)}")
            else:
                await event.reply("⚠️ Формат: `#79001112233/текст`", parse_mode='md')
        
        # --- БЛОК КЛИЕНТА ---
        else:
            # Сначала получаем данные отправителя, чтобы избежать ошибки
            sender = await event.get_sender()
            sender_phone = getattr(sender, 'phone', None) # Получаем телефон
            
            # Сохраняем файл (если он есть)
            f_url = await save_tg_media(event)
            
            await log_to_db(
                source="Client", 
                phone=sender_phone or "Unknown", 
                client_name=full_name,
                tg_client_id=tg_client_id,
                text=raw_text or "[Файл]", 
                sender=sender_phone, 
                f_url=f_url,
                direction="in", 
                status="pending",
                tg_id=event.message.id  # Теперь и ID сообщения на месте
            )

@app.before_serving
async def startup():
    await init_db()
    asyncio.create_task(start_listener())

# --- API ЭНДПОИНТЫ ДЛЯ 1С ---

@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+').strip()
    text, c_id, c_name = data.get("text", ""), data.get("client_id"), data.get("client_name")
    tg = await get_client()
    try:
        sent = await tg.send_message(phone, text)
        await log_to_db("1C", phone, text, sender="system_1c", c_id=c_id, c_name=c_name, direction="out", tg_id=sent.id)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+').strip()
    f_url, text = data.get("file"), data.get("text", "")
    c_id, c_name = data.get("client_id"), data.get("client_name")
    tg = await get_client()
    try:
        sent = await tg.send_file(phone, f_url, caption=text)
        await log_to_db("1C", phone, text, sender="system_1c", f_url=f_url, c_id=c_id, c_name=c_name, direction="out", tg_id=sent.id)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/fetch_new', methods=['GET', 'POST'])
async def fetch_new():
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM outbound_logs WHERE status = 'pending'") as c:
            rows = [dict(r) for r in await c.fetchall()]
        if rows:
            print(f"📤 ОТДАЕМ В 1С {len(rows)} ЗАПИСЕЙ")
            ids = [r['id'] for r in rows]
            await db.execute(f"UPDATE outbound_logs SET status='ok' WHERE id IN ({','.join(['?']*len(ids))})", ids)
            await db.commit()
        return jsonify(rows)

@app.route('/get_file/<filename>')
async def get_file(filename): 
    return await send_from_directory(FILES_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
