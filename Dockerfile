FROM python:3.11-slim

WORKDIR /app

# Зависимости кэшируются отдельным слоем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Токен и режим задаются при запуске:
#   docker run -e WB_BOT_TOKEN=... -e WB_DEMO_MODE=1 ...
CMD ["python", "bot.py"]
