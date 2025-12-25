FROM python:3.9-slim

# 1. Устанавливаем системные зависимости (нужны для некоторых библиотек Python)
RUN apt-get update && apt-get install -y \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# 2. Создаем рабочую директорию
WORKDIR /app

# 3. Сначала копируем только список библиотек (это ускоряет сборку в будущем)
COPY requirements.txt .

# 4. Устанавливаем библиотеки от имени root, чтобы точно всё поставилось
RUN pip install --no-cache-dir -r requirements.txt

# 5. Копируем ВЕСЬ остальной код проекта в образ
COPY . .

# 6. Создаем нужные папки и даем на них права (чтобы база и файлы работали)
RUN mkdir -p /app/data /app/files && chmod -R 777 /app/data /app/files

# 7. Открываем порт для Quart (1С будет стучаться сюда)
EXPOSE 5000

# 8. Запуск бота
CMD ["python", "app.py"]
