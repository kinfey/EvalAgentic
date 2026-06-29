FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir "flask>=3.0"

COPY app.py .
COPY templates ./templates

ENV PORT=8082
EXPOSE 8082

CMD ["python", "app.py"]
