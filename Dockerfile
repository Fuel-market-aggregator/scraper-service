FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# Folosim portul 8001 ca sa nu se bata cap in cap cu io-service (care e pe 8000) cand testam local
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]