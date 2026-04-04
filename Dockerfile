FROM python:3.13-slim

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*
RUN useradd -r -s /bin/false appuser

WORKDIR /app

COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY api/ ./api/

USER appuser
EXPOSE 8080

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080"]
