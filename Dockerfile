FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot/
COPY VERSION .
ARG GIT_COMMIT=unknown
LABEL org.opencontainers.image.revision=$GIT_COMMIT \
      org.opencontainers.image.source="https://github.com/Xoudusz/vibecode-bot"
CMD ["uvicorn", "bot.server:app", "--host", "0.0.0.0", "--port", "8080"]
