# Build stage
FROM python:3.10-alpine as builder

WORKDIR /app


RUN apk add --no-cache gcc musl-dev cargo rust

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Runtime stage
FROM python:3.10-alpine

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY main.py .

RUN mkdir -p /app/temp && chmod 777 /app/temp

CMD ["python", "main.py"]
