# Используем легкий Python 3.9
FROM python:3.9-slim

# Устанавливаем рабочую директорию
WORKDIR /app

# ОБНОВЛЕННЫЙ БЛОК:
# libgl1-mesa-glx устарел, используем libgl1 и mesa-utils
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    mesa-utils \
    && rm -rf /var/lib/apt/lists/*

# Копируем файл с зависимостями
COPY requirements.txt .

# Устанавливаем библиотеки Python
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код в контейнер
COPY . .

# Открываем порт 8000
EXPOSE 8000

# Команда запуска
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]