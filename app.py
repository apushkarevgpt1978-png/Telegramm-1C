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
GROUP_ID = int(os.environ.get('TELEGRAM_GROUP_ID', '-1003599844429')) # Твоя группа
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
        # Таблица логов для 1С
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, phone TEXT, client_name TEXT, client_id TEXT,
                sender_number TEXT, messenger TEXT DEFAULT 'tg', message_text TEXT,
                file_url TEXT, status TEXT DEFAULT 'pending', tg_message_id INTEGER,
                direction TEXT, error_text TEXT, created_at DATETIME, manager TEXT
            )
        """)
        # Таблица для связи ТЕМ и ТЕЛЕФОНОВ
        await db.execute("CREATE TABLE IF NOT EXISTS topics (topic_id INTEGER PRIMARY KEY, phone TEXT)")
        try: await db.execute("ALTER TABLE outbound_logs ADD COLUMN manager TEXT")
        except: pass 
        await db.commit()

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager=None, s_number=None, f_url=None, direction='in', tg_id=None):
    messenger = 'tg'
    created_at = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, phone, c_name, c_id, manager, s_number, messenger, text, f_url, 'pending', direction, tg_id, created_at))
            await db.commit()
    except Exception as e: print(f"⚠️ ОШИБКА БД: {e}")

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
        # ЭТО ПОКАЖЕТ В ЛОГАХ ПРИШЛО ЛИ СООБЩЕНИЕ
        print(f"DEBUG: Получено сообщение от {event.sender_id} в чате {event.chat_id}")
        
        # --- 1. ВХОДЯЩЕЕ ОТ КЛИЕНТА (В ЛИЧКУ БОТУ) ---
        if event.is_private:
            sender = await event.get_sender()
            s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
            
            # Если пишет менеджер в личку - просто логируем и выходим
            if s_phone in managers_list: 
                print(f"DEBUG: Менеджер {s_phone} написал в личку, игнорим.")
                return 

            s_id = str(event.sender_id)
            s_full_name = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip() or "Unknown"
            raw_text = (event.raw_text or "").strip()
            print(f"DEBUG: Входящее от клиента {s_phone}: {raw_text}")
            
            f_url = await save_tg_media(event)

            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute('SELECT topic_id FROM topics WHERE phone=?', (s_phone,)) as cursor:
                    row = await cursor.fetchone()
                    if row: 
                        topic_id = row[0]
                    else:
                        print(f"DEBUG: Создаю тему для {s_phone}")
                        try:
                            res = await tg(functions.channels.CreateForumTopicRequest(channel=GROUP_ID, title=f"{s_full_name} ({s_phone})"))
                            topic_id = res.updates[0].id
                            await db.execute('INSERT INTO topics (topic_id, phone) VALUES (?, ?)', (topic_id, s_phone))
                            await db.commit()
                        except Exception as e:
                            print(f"DEBUG: Ошибка создания темы: {e}")
                            topic_id = None

            await tg.send_message(GROUP_ID, f"**{s_full_name}** ({s_phone}):\n{raw_text}", reply_to=topic_id)
            await log_to_db(source="Client", phone=s_phone, text=raw_text, c_name=s_full_name, c_id=s_id, f_url=f_url, direction="in", tg_id=event.message.id)

        # --- 2. СООБЩЕНИЕ ИЗ ГРУППЫ (ОТ МЕНЕДЖЕРА) ---
        elif event.chat_id == GROUP_ID:
            print(f"DEBUG: Сообщение в группе от менеджера")
            if event.out: return
        # --- 2. СООБЩЕНИЕ ИЗ ГРУППЫ (ОТ МЕНЕДЖЕРА) ---
        elif event.chat_id == GROUP_ID:
            if event.out: return # Игнорим свои сообщения
            
            raw_text = (event.raw_text or "").strip()
            sender = await event.get_sender()
            mgr_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()

            # А. Ответ в теме клиента
            if event.reply_to_msg_id:
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute('SELECT phone FROM topics WHERE topic_id=?', (event.reply_to_msg_id,)) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            target_phone = row[0]
                            sent = await tg.send_message(target_phone, raw_text)
                            await event.reply(f"✅ Отправлено клиенту {target_phone}")
                            await log_to_db(source="Manager", phone=target_phone, text=raw_text, manager=mgr_phone, direction="out", tg_id=sent.id)
                            return

            # Б. Маска в общем чате или подсказка
            match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
            if match:
                target_phone, message_text = match.group(1).strip(), match.group(2).strip()
                sent = await tg.send_message(target_phone, message_text)
                await event.reply(f"✅ Отправлено на {target_phone}")
                await log_to_db(source="Manager", phone=target_phone, text=message_text, manager=mgr_phone, direction="out", tg_id=sent.id)
            elif not event.reply_to_msg_id:
                await event.reply("ℹ️ Пишите в теме клиента для ответа или используйте `#номер/текст` в общем чате.")



# --- Остальные роуты (/send, /send_file, /fetch_new) оставляем без изменений ---
@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+').strip()
    text, mgr = data.get("text", ""), data.get("manager")
    tg = await get_client()
    try:
        sent = await tg.send_message(phone, text)
        await log_to_db(source="1C", phone=phone, text=text, manager=mgr, direction="out", tg_id=sent.id)
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone = str(data.get("phone", "")).lstrip('+').strip()
    f_url, text, mgr = data.get("file"), data.get("text", ""), data.get("manager")
    tg = await get_client()
    try:
        sent = await tg.send_file(phone, f_url, caption=text)
        await log_to_db(source="1C", phone=phone, text=text, manager=mgr, f_url=f_url, direction="out", tg_id=sent.id)
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

@app.before_serving
async def startup():
    await init_db()
    # Мы не создаем таск, а просто запускаем проверку клиента
    print("DEBUG: Инициализация БД завершена")

async def run_bot():
    tg = await get_client()
    print("DEBUG: Клиент Telegram получен")
    
    # Прямо здесь запускаем листенер
    await start_listener() 
    print("Бот ГЕНА запущен в режиме ТЕМ!")
    await tg.run_until_disconnected()

if __name__ == '__main__':
    # Чтобы Quart и Telethon работали вместе стабильно
    import asyncio
    loop = asyncio.get_event_loop()
    
    # Инициализируем БД
    loop.run_until_complete(init_db())
    
    # Запускаем бота фоном
    print("DEBUG: Запуск бота...")
    loop.create_task(start_listener()) 
    
    # Запускаем веб-сервер
    app.run(host='0.0.0.0', port=5000)
