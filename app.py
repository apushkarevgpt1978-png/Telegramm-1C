from quart import Quart, request, jsonify, send_file
import os
import hashlib
import mimetypes
import asyncio
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact

app = Quart(__name__)

# --- НАСТРОЙКИ (забираем из переменных окружения) ---
# Если переменная не найдена, используем None или дефолтное значение
API_ID = os.getenv('TG_API_ID')
API_HASH = os.getenv('TG_API_HASH')

# Пути можно оставить такими или тоже вынести, если они меняются
SESSION_PATH = os.getenv('TG_SESSION_PATH', '/app/GenaAPI')
TEMP_STORAGE = os.getenv('TG_TEMP_STORAGE', '/tmp/telegram_files')

os.makedirs(TEMP_STORAGE, exist_ok=True)
async def get_client():
    client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
    await client.connect()
    return client

async def process_media(client, message):
    if not message.media:
        return None
    try:
        file_hash = hashlib.md5(f"{message.id}_{message.date.timestamp()}".encode()).hexdigest()[:8]
        mime = None
        if hasattr(message.media, 'document'):
            mime = message.media.document.mime_type
        elif hasattr(message.media, 'photo'):
            mime = "image/jpeg"
            
        extension = mimetypes.guess_extension(mime) if mime else ""
        if not extension and mime == "image/webapp": extension = ".png"

        attr_name = "file"
        if hasattr(message.media, 'document'):
            for attr in message.media.document.attributes:
                if hasattr(attr, 'file_name'):
                    attr_name = os.path.splitext(attr.file_name)[0]
                    break
        
        filename = f"{file_hash}_{attr_name}{extension}"
        save_path = os.path.join(TEMP_STORAGE, filename)
        path = await client.download_media(message, file=save_path)
        
        if path:
            return {
                "url": f"http://{request.host}/download/{filename}",
                "filename": filename,
                "filesize": os.path.getsize(path),
                "mimetype": mime or "application/octet-stream"
            }
    except Exception as e:
        print(f"Media error: {e}")
    return None

@app.route('/send', methods=['POST'])
async def send_telegram():
    data = await request.get_json()
    phone, text = data.get("phone", ""), data.get("text", "")
    client = await get_client()
    try:
        if not await client.is_user_authorized():
            return jsonify({"error": "Auth required"}), 401
        sent = await client.send_message(phone, text)
        return jsonify({
            "status": "sent",
            "message_id": sent.id,
            "chat_id": sent.chat_id,
            "user_id": getattr(sent.peer_id, 'user_id', None)
        }), 200
    finally:
        await client.disconnect()

@app.route('/send_url', methods=['POST'])
async def send_url_telegram():
    data = await request.get_json()
    phone = data.get("phone", "").strip()
    file_url = data.get("file_url", "").strip()
    caption = data.get("caption", "").strip()
    client = await get_client()
    try:
        if not await client.is_user_authorized():
            return jsonify({"error": "Auth required"}), 401
        sent = await client.send_file(phone, file_url, caption=caption)
        return jsonify({
            "status": "sent",
            "message_id": sent.id,
            "chat_id": sent.chat_id,
            "user_id": getattr(sent.peer_id, 'user_id', None)
        }), 200
    finally:
        await client.disconnect()

@app.route('/get_messages', methods=['POST'])
async def get_messages():
    data = await request.get_json() or {}
    limit_dialogs = int(data.get("limit_dialogs", 10))
    only_unread = data.get("only_unread", False)
    client = await get_client()
    try:
        if not await client.is_user_authorized():
            return jsonify({"error": "Auth required"}), 401
        all_messages = []
        async for dialog in client.iter_dialogs(limit=limit_dialogs):
            if not dialog.is_user or dialog.id == 777000: continue
            count_to_fetch = dialog.unread_count if only_unread else int(data.get("limit_messages", 5))
            if count_to_fetch == 0 and only_unread: continue
            read_max_id = getattr(dialog.dialog, 'read_inbox_max_id', 0)
            async for msg in client.iter_messages(dialog.id, limit=count_to_fetch):
                media_data = await process_media(client, msg)
                all_messages.append({
                    "message_id": msg.id,
                    "sender_id": msg.sender_id,
                    "chat_id": dialog.id,
                    "chat_name": dialog.name,
                    "text": msg.text or "",
                    "date": msg.date.isoformat(),
                    "is_out": msg.out,
                    "is_unread": (not msg.out) and (msg.id > read_max_id),
                    "media_info": media_data
                })
        return jsonify({"status": "success", "messages": all_messages}), 200
    finally:
        await client.disconnect()

@app.route('/download/<filename>', methods=['GET'])
async def download_file(filename):
    path = os.path.join(TEMP_STORAGE, filename)
    if os.path.exists(path): return await send_file(path)
    return "File not found", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
