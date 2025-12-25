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
                client_name TEXT
            )
        """)
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
    except Exception as e:
        print(f"⚠️ ОШИБКА БД: {e}")

async def get_topic_from_db(c_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT topic_id FROM client_topics WHERE client_id = ?", (str(c_id),)) as cursor:
            row = await cursor.fetchone()
            return row['topic_id'] if row else None

async def delete_broken_topic(topic_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM client_topics WHERE topic_id = ?", (topic_id,))
        await db.commit()
    print(f"🗑️ Удалена битая ссылка на тему: {topic_id}")

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

        # --- 1. ЛОГИКА МЕНЕДЖЕРА ---
        if s_phone in managers_list:
            if raw_text.startswith('#'):
                match = re.search(r'#(\d+)/(.*)', raw_text, re.DOTALL)
                if not match:
                    await event.reply("❌ Ошибка! Чтобы создать диалог, заполни маску верно.\nПример для копирования:\n`#79153019495/ИванИванович`")
                    return
                
                target_phone, content = match.group(1).strip(), match.group(2).strip()
                try:
                    ent = await tg.get_entity(target_phone)
                    c_id = str(ent.id)
                    topic_id = await get_topic_from_db(c_id)
                    
                    if not topic_id:
                        display_title = f"{target_phone} {content}"
                        result = await tg(functions.messages.CreateForumTopicRequest(peer=GROUP_ID, title=display_title))
                        topic_id = next((u.id for u in result.updates if hasattr(u, 'id')), None)
                        
                        if topic_id:
                            async with aiosqlite.connect(DB_PATH) as db:
                                await db.execute("INSERT OR REPLACE INTO client_topics (client_id, topic_id, client_name) VALUES (?, ?, ?)",
                                               (c_id, topic_id, content))
                                await db.commit()
                            await event.reply(f"✅ Диалог создан! ID: {topic_id}. Теперь сообщения будут приходить в отдельную ветку.")
                    else:
                        try:
                            f_url = await save_tg_media(event)
                            sent = await (tg.send_file(ent.id, os.path.join(FILES_DIR, f_url.split('/')[-1]), caption=content) if f_url else tg.send_message(ent.id, content))
                            await log_to_db(source="Manager", phone=target_phone, text=content, c_id=c_id, manager=s_phone, f_url=f_url, direction="out", tg_id=sent.id)
                            
                            try:
                                await tg.send_message(GROUP_ID, f"📤 Мой ответ: {content}", reply_to=topic_id)
                                await event.reply("✅ Отправлено и добавлено в диалог")
                            except Exception as topic_e:
                                if "reply_to_msg_id_invalid" in str(topic_e).lower() or "deleted" in str(topic_e).lower():
                                    await delete_broken_topic(topic_id)
                                    await event.reply("⚠️ Тема была удалена в Telegram. База очищена. Повторите маску для создания новой темы.")
                                else:
                                    await event.reply(f"✅ Отправлено клиенту, но не добавлено в группу (Ошибка: {topic_e})")
                        except Exception as inner_e:
                            await event.reply(f"❌ Ошибка отправки: {str(inner_e)}")
                except Exception as e:
                    if "entity" in str(e).lower(): await event.reply("❌ Пользователь не найден")
                    else: await event.reply(f"❌ Ошибка: {str(e)}")
                return

            if event.is_group and event.reply_to:
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    async with db.execute("SELECT client_id FROM client_topics WHERE topic_id = ?", (event.reply_to_msg_id
