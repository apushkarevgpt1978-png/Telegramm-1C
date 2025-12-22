FROM python:3.9-slim

RUN useradd -m appuser
USER appuser

WORKDIR /app

COPY --chown=appuser requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser app.py .

EXPOSE 5000

RUN rm -f /app/data/gateway_messages.db
CMD ["python", "app.py"]
