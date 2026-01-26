# 1. Берем легкий Linux с Python 3.9
FROM python:3.9-slim

# 2. Устанавливаем рабочую папку
WORKDIR /app

# 3. ЭТО ВАЖНО: Устанавливаем системные библиотеки для ddddocr и fitz
# Без этого сервер упадет с ошибкой "libGL.so not found"
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 4. Копируем список библиотек и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Копируем весь твой код (main.py и остальное)
COPY . .

# 6. Открываем порт
EXPOSE 8000

# 7. Команда для запуска сервера
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]