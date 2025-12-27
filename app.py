import os, asyncio, aiosqlite, re, uuid, httpx
from datetime import datetime
from quart import Quart, request, jsonify, send_from_directory
from telethon import TelegramClient, events, functions, types, errors

app = Quart(__name__)

# --- CONFIG ---
# Данные Telegram
API_ID = int(os.environ.get('API_ID', 0))
API_HASH = os.environ.get('API_HASH', '')

# Данные для Green-API (WhatsApp)
# Добавляем пустую строку как значение по умолчанию, чтобы код не упал при запуске
WA_ID_INSTANCE = os.environ.get("WA_ID_INSTANCE", "")
WA_API_TOKEN = os.environ.get("WA_API_TOKEN", "")

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
        # Убедись, что SESSION_PATH ведет на GenaAPI без папок, если файл лежит в корне
        client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    
    if not client.is_connected():
        await client.connect()
    
    # Проверка: если файл сессии не подхватился, не пытаемся запустить ввод телефона
    if not await client.is_user_authorized():
        print("❌ ОШИБКА: Сервер не авторизован. Файл .session не найден или не валиден.")
        # Здесь мы НЕ вызываем start(), чтобы не вешать сервер
    return client

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица логов: добавили topic_id
        await db.execute("""
            CREATE TABLE IF NOT EXISTS outbound_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT, 
                phone TEXT, 
                client_name TEXT, 
                client_id TEXT,
                sender_number TEXT, 
                messenger TEXT DEFAULT 'tg', 
                message_text TEXT,
                file_url TEXT, 
                status TEXT DEFAULT 'pending', 
                tg_message_id INTEGER,
                topic_id INTEGER, -- Наша новая колонка
                direction TEXT, 
                error_text TEXT, 
                created_at DATETIME, 
                manager TEXT
            )
        """)
        
        # Таблица тем: добавили messenger
        await db.execute("""
            CREATE TABLE IF NOT EXISTS client_topics (
                client_id TEXT PRIMARY KEY, 
                topic_id INTEGER,
                client_name TEXT, 
                phone TEXT, 
                manager_ref TEXT,
                messenger TEXT DEFAULT 'tg' -- Наша новая колонка
            )
        """)
        await db.commit()
        print("✅ База данных (структура) актуализирована")

async def log_to_db(source, phone, text, c_name=None, c_id=None, manager_fio=None, s_number=None, f_url=None, direction='in', tg_id=None, topic_id=None, messenger='tg'):
    """Логирует сообщение в базу данных, включая ID темы (topic_id)"""
    created_at = datetime.now()
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute("""
                INSERT INTO outbound_logs 
                (source, phone, client_name, client_id, manager, sender_number, messenger, message_text, file_url, status, direction, tg_message_id, topic_id, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                str(source), 
                str(phone or ""), 
                str(c_name or ""), 
                str(c_id or ""), 
                str(manager_fio or ""), 
                str(s_number or ""), 
                str(messenger), # Теперь передаем переменную вместо жесткого 'tg'
                str(text or ""), 
                f_url, 
                'pending', 
                direction, 
                tg_id, 
                topic_id, # Добавленный ID темы
                created_at
            ))
            await db.commit()
    except Exception as e: 
        print(f"⚠️ DB Error: {e}")

async def create_new_topic(client_id, client_name, messenger='tg'):
    try:
        tg = await get_client()
        
        # Красивый заголовок с учетом твоей просьбы не дублировать номер
        if str(client_id) == str(client_name) or "Клиент" in str(client_name):
            topic_title = str(client_name)
        else:
            topic_title = f"{client_name} ({client_id})"
            
        new_topic_id = None
        print(f"🛠 Создание темы: {topic_title} в группе {GROUP_ID}...")

        try:
            # Пытаемся создать тему через API
            result = await tg(functions.messages.CreateForumTopicRequest(
                peer=GROUP_ID,
                title=topic_title
            ))
            # Достаем ID темы из любого типа ответа
            for update in result.updates:
                if hasattr(update, 'id'): 
                    new_topic_id = update.id
                    break
        except Exception as e:
            print(f"⚠️ Ошибка API при создании: {e}")

        # Страховка через историю (если API не вернуло ID сразу)
        if not new_topic_id:
            await asyncio.sleep(2)
            async for msg in tg.iter_messages(GROUP_ID, limit=15):
                if hasattr(msg, 'action') and isinstance(msg.action, types.MessageActionTopicCreate):
                    if str(client_id) in msg.action.title:
                        new_topic_id = msg.id
                        break

        if new_topic_id:
            # ЗАПИСЬ В БАЗУ: теперь вносим и group_id
            async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                await db.execute("""
                    INSERT OR REPLACE INTO client_topics 
                    (client_id, topic_id, client_name, messenger, group_id)
                    VALUES (?, ?, ?, ?, ?)
                """, (str(client_id), new_topic_id, str(client_name), messenger, str(GROUP_ID)))
                await db.commit()
            
            print(f"✅ Тема {new_topic_id} привязана к группе {GROUP_ID} в базе")
            return new_topic_id
        
        return None
            
    except Exception as e:
        print(f"❌ Критическая ошибка в create_new_topic: {e}")
        return None

# Обработчик сервисных действий в чате (удаление тем)
@client.on(events.ChatAction)
async def handler_chat_action(event):
    try:
        # Проверяем, является ли действие удалением темы форума
        if event.is_group and event.action_deleted:
            # Получаем ID удаленной темы
            # В Telethon удаление темы часто приходит через event.action_message.id или event.message.id
            deleted_topic_id = getattr(event.action_message, 'id', None)
            
            if deleted_topic_id:
                async with aiosqlite.connect(DB_PATH, timeout=10) as db:
                    # Удаляем запись из базы по topic_id
                    cursor = await db.execute(
                        "DELETE FROM client_topics WHERE topic_id = ?", 
                        (deleted_topic_id,)
                    )
                    await db.commit()
                    
                    if cursor.rowcount > 0:
                        print(f"🗑 Тема {deleted_topic_id} удалена из Telegram и очищена в БД")
    except Exception as e:
        print(f"⚠️ Ошибка при обработке удаления темы: {e}")

# Дополнительный обработчик для точного отлова удаления тем (через Raw Updates)
@client.on(events.Raw(types.UpdateTimeline) if hasattr(types, 'UpdateTimeline') else events.Raw())
async def raw_handler(update):
    if isinstance(update, types.UpdateDeleteMessages):
        # Если удаляются сообщения, проверяем, не были ли это сервисные сообщения тем
        async with aiosqlite.connect(DB_PATH) as db:
            for msg_id in update.messages:
                await db.execute("DELETE FROM client_topics WHERE topic_id = ?", (msg_id,))
            await db.commit()

async def get_topic_info_with_retry(phone_number):
    clean_phone = str(''.join(filter(str.isdigit, str(phone_number))))
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM client_topics WHERE client_id = ?", (clean_phone,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None  # Клиента вообще нет в базе

            client_data = dict(row)
            try:
                tg = await get_client()
                res = await tg.get_messages(GROUP_ID, ids=int(client_data['topic_id']))
                # Если тема в ТГ "битая" или пустая
                if not res or isinstance(res, types.MessageEmpty):
                    client_data['topic_id'] = None # Сигнал к пересозданию
                return client_data
            except Exception:
                # Если ТГ недоступен, возвращаем что есть в базе
                return client_data

async def find_last_outbound_manager(c_id):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT manager FROM outbound_logs 
                WHERE client_id = ? AND direction = 'out' AND manager != '' 
                ORDER BY created_at DESC LIMIT 1
            """, (str(c_id),)) as cursor:
                row = await cursor.fetchone()
                return row['manager'] if row else ""
    except: return ""

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
                print(f"🗑️ Тема {t_id} удалена из БД по событию TG")

    @tg.on(events.NewMessage())
    async def handler(event):
        if event.out and not event.is_group: return # Игнорируем свои исходящие в личке

        sender = await event.get_sender()
        s_id = str(event.sender_id)
        raw_text = (event.raw_text or "").strip()

        # 1. ГРУППА (МЕНЕДЖЕР -> КЛИЕНТУ)
        if event.is_group:
            s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
            
            # Создание темы
            if raw_text.startswith('#'):
                match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
                if not match:
                    await event.reply("❌ Неверный формат!\nПример: `#79876543210/Иванов Иван`")
                    return
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
                        await event.reply(f"✅ Тема создана для {t_phone}")
                except Exception as e: await event.reply(f"❌ Ошибка: {str(e)}")
                return

            # Пересылка из темы клиенту
            if event.reply_to_msg_id:
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("SELECT * FROM client_topics WHERE topic_id = ?", (event.reply_to_msg_id,)) as c:
                        row = await c.fetchone()
                
                if row:
                    try:
                        target_id = int(row['client_id'])
                        target_ent = await tg.get_entity(target_id) # Обязательно для access_hash
                        f_url = await save_tg_media(event)
                        
                        if event.message.media: sent = await tg.send_file(target_ent, event.message.media, caption=raw_text)
                        else: sent = await tg.send_message(target_ent, raw_text)
                        
                        m_fio = MANAGERS.get(s_phone, s_phone)
                        await log_to_db(source="Manager", phone=row['phone'], c_name=row['client_name'], text=raw_text, c_id=str(target_id), manager_fio=m_fio, s_number=s_phone, f_url=f_url, direction="out", tg_id=sent.id)
                        print(f"➡️ [OUT] Из темы в личку клиенту {target_id}")
                    except Exception as e: print(f"❌ Ошибка OUT: {e}")
            return

        # 2. ЛИЧКА (КЛИЕНТ -> В ТЕМУ)
        if event.is_private:
            f_url = await save_tg_media(event)
            s_phone = str(getattr(sender, 'phone', '') or '').lstrip('+').strip()
            s_full_name = f"{getattr(sender, 'first_name', '') or ''} {getattr(sender, 'last_name', '') or ''}".strip() or "Client"
            
            row = await get_topic_info_with_retry(s_id)
            if row:
                msg_source = "Manager"; m_fio = MANAGERS.get(row['manager_ref'], ""); m_phone = row['manager_ref']
                try:
                    if event.message.media: await tg.send_file(GROUP_ID, event.message.media, caption=f"📎 {raw_text}", reply_to=row['topic_id'])
                    elif raw_text: await tg.send_message(GROUP_ID, f"💬 {raw_text}", reply_to=row['topic_id'])
                    print(f"⬅️ [IN] Из лички в тему {row['topic_id']}")
                except Exception as e: print(f"❌ Ошибка IN в тему: {e}")
            else:
                msg_source = "1C"; m_fio = await find_last_outbound_manager(s_id); m_phone = ""
                print(f"⬅️ [IN] Темы нет, менеджер из истории: {m_fio}")
            
            await log_to_db(source=msg_source, phone=s_phone, text=raw_text, c_name=s_full_name, c_id=s_id, manager_fio=m_fio, s_number=m_phone, f_url=f_url, direction="in", tg_id=event.message.id)

# --- API ROUTES ---
@app.route('/send', methods=['POST'])
async def send_text():
    data = await request.get_json()
    
    # Парсим основные данные
    phone = str(data.get("phone", "")).lstrip('+').strip()
    text = data.get("text", "")
    mgr_fio = str(data.get("manager", ""))
    
    # --- ЛОГИКА ИМЕНИ ---
    # Если 1С прислала пустоту или не прислала ничего, будет "Клиент 79xxxxxxxxx"
    c_name = data.get("client_name")
    if not c_name or str(c_name).strip() == "":
        c_name = f"Клиент {phone}"
    # ---------------------

    messenger = str(data.get("messenger", "tg")).lower()

    # Ищем или создаем тему
    topic_info = await get_topic_info_with_retry(phone)
    if topic_info and topic_info.get('topic_id'):
        topic_id = topic_info['topic_id']
    else:
        # Передаем уже проверенное имя c_name
        topic_id = await create_new_topic(phone, c_name, messenger=messenger)

    if not topic_id:
        return jsonify({"error": "Не удалось создать ветку в Telegram"}), 500

    try:
        # 3. РАЗВИЛКА: WhatsApp или Telegram
        if any(word in messenger for word in ["wa", "whatsapp", "вотсап"]):
            # --- ОТПРАВКА В WHATSAPP ---
            success, msg_id = await send_whatsapp_message(phone, text)
            used_messenger = "wa"
            
            if success:
                # ДУБЛИРУЕМ В TELEGRAM TOPIC (без имени менеджера)
                tg = await get_client()
                wa_report = (
                    f"🟢 **Отправлено в WhatsApp**\n\n"
                    f"{text}"
                )
                await tg.send_message(GROUP_ID, wa_report, reply_to=topic_id)
        
        else:
            # --- ОТПРАВКА В TELEGRAM ---
            tg = await get_client()
            sent = await tg.send_message(GROUP_ID, text, reply_to=topic_id)
            success, msg_id = True, sent.id
            used_messenger = "tg"

        # 4. ЛОГИРОВАНИЕ
        if success:
            await log_to_db(
                source="1C", 
                phone=phone, 
                text=text, 
                manager_fio=mgr_fio, 
                direction="out", 
                tg_id=msg_id, 
                topic_id=topic_id, 
                messenger=used_messenger
            )
            return jsonify({"status": "ok", "topic_id": topic_id}), 200
        else:
            # Отчет об ошибке в тему тоже сделаем коротким
            tg = await get_client()
            await tg.send_message(GROUP_ID, f"🔴 **Ошибка WhatsApp!**\n{msg_id}", reply_to=topic_id)
            return jsonify({"error": msg_id}), 400

    except Exception as e:
        print(f"❌ Ошибка в send_text: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/send_file', methods=['POST'])
async def send_file():
    data = await request.get_json()
    phone, f_url, text, mgr_fio = str(data.get("phone", "")).lstrip('+').strip(), data.get("file"), data.get("text", ""), str(data.get("manager", ""))
    tg = await get_client()
    try:
        ent = await tg.get_entity(phone)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM client_topics WHERE client_id = ?", (str(ent.id),))
            await db.commit()
        sent = await tg.send_file(ent, f_url, caption=text)
        await log_to_db(source="1C", phone=phone, c_name=f"{ent.first_name or ''}", text=text, c_id=str(ent.id), manager_fio=mgr_fio, f_url=f_url, direction="out", tg_id=sent.id)
        print(f"🚀 [API] Файл отправлен клиенту {ent.id}")
        return jsonify({"status": "ok"}), 200
    except Exception as e: return jsonify({"error": str(e)}), 500

async def send_whatsapp_message(phone, text):
    """Отправляет сообщение через Green-API, используя httpx"""
    url = f"https://api.green-api.com/waInstance{WA_ID_INSTANCE}/sendMessage/{WA_API_TOKEN}"
    payload = {
        "chatId": f"{phone}@c.us",
        "message": text
    }
    
    try:
        # В httpx используется AsyncClient
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            
            if response.status_code == 200:
                result = response.json()
                return True, result.get("idMessage")
            else:
                return False, f"Ошибка WA: {response.status_code} - {response.text}"
                
    except Exception as e:
        print(f"❌ Исключение при отправке WA: {e}")
        return False, str(e)

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

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
