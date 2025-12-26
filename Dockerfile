FROM python:3.11-slim

# Disable Python output buffering for real-time logs
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY draft_service.py .

CMD ["python", "draft_service.py"]
