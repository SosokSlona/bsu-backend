cd backend
rm -rf venv

# 1. Ищем стабильный Python (3.10 или 3.9), избегая экспериментального 3.14
PYTHON_EXEC=""
if [ -f "/opt/homebrew/bin/python3.10" ]; then
    PYTHON_EXEC="/opt/homebrew/bin/python3.10"
elif command -v python3.10 &> /dev/null; then
    PYTHON_EXEC=$(command -v python3.10)
elif command -v python3.9 &> /dev/null; then
    PYTHON_EXEC=$(command -v python3.9)
else
    # Если ничего нет, используем то, что есть, но предупреждаем
    PYTHON_EXEC=$(command -v python3)
    echo "ВНИМАНИЕ: Используется стандартный python3. Если это версия 3.14, установка может упасть."
fi

echo "--- Создаем окружение с помощью: $PYTHON_EXEC ---"

# 2. Создаем venv
$PYTHON_EXEC -m venv venv
source venv/bin/activate

# 3. Обновляем pip (Критично для Apple Silicon!)
pip install --upgrade pip

# 4. Устанавливаем зависимости
pip install -r requirements.txt

echo "---------------------------------------------------"
echo "Установка завершена."
echo "ТЕПЕРЬ В ВАЖНОЕ ДЕЙСТВИЕ В VS CODE:"
echo "1. Нажмите F1 (или Cmd+Shift+P)."
echo "2. Введите: Python: Select Interpreter"
echo "3. Выберите пункт, где есть путь: ./backend/venv/bin/python"
echo "   (Если его нет, нажмите 'Enter interpreter path' -> 'Find' и выберите этот файл вручную)"
echo "---------------------------------------------------"
