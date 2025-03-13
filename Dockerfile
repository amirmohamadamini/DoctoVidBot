FROM python:3.10-alpine

WORKDIR /app

RUN apk add --no-cache gcc musl-dev cargo rust

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

RUN mkdir -p /app/temp && chmod 777 /app/temp




CMD ["python", "main.py"]