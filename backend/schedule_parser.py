import pdfplumber
import re
import io
from typing import List, Dict
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
# Время: 09:00-10:20 (учитываем точки и двоеточия)
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
# Аудитория: 3-4 цифры, возможно с буквой, или с/з, с/к, ауд
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к|ауд\.?)\b', re.IGNORECASE)
# Тип занятия: (лек), (прак), (сем) и т.д.
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?)\)', re.IGNORECASE)
# Преподаватель: Фамилия И.О.
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    # Логика выбора страниц:
    # 1 курс -> страницы 0, 1
    # 2 курс -> страницы 2, 3
    # ...
    # Если курс 0 или меньше (ошибка определения), берем первые две
    if course < 1: course = 1
    start_page_idx = (course - 1) * 2
    end_page_idx = start_page_idx + 2

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total_pages = len(pdf.pages)
        
        # Если страниц меньше, чем мы насчитали, берем что есть, но не выходим за границы
        pages_to_parse = []
        for i in range(start_page_idx, min(end_page_idx, total_pages)):
            pages_to_parse.append(pdf.pages[i])
            
        # Fallback: Если список пуст (например, PDF всего 1 стр, а мы ищем 4-ю), берем всё
        if not pages_to_parse:
            pages_to_parse = pdf.pages

        for page in pages_to_parse:
            # Настройки extract_table:
            # vertical_strategy="lines" идеально для сеток БГУ
            # snap_tolerance чуть выше, чтобы ловить кривые линии
            table = page.extract_table({
                "vertical_strategy": "lines", 
                "horizontal_strategy": "lines",
                "snap_tolerance": 5,
            })
            
            if not table or len(table) < 2: continue

            # --- 1. АНАЛИЗ ШАПКИ (Header) ---
            headers = table[0]
            time_col_idx = -1
            group_map = {} # {col_index: "Group Name"}
            
            for i, h in enumerate(headers):
                if h is None: continue
                # Чистим текст шапки от мусора и перевертышей
                h_clean = _fix_text_issues(h).lower()
                
                if "время" in h_clean:
                    time_col_idx = i
                # Если это не "День" и не "Время" и текст длинный - скорее всего название группы
                elif "день" not in h_clean and len(h_clean) > 2:
                    raw_group = _fix_text_issues(h).replace('\n', ' ').strip()
                    group_map[i] = raw_group
                    if raw_group not in schedule_by_group:
                        schedule_by_group[raw_group] = {}

            # Fallback: если колонку времени не нашли по слову "Время", считаем что это 2-я колонка (индекс 1)
            if time_col_idx == -1: time_col_idx = 1
            
            current_day = "Понедельник"
            
            # --- 2. АНАЛИЗ СТРОК (Rows) ---
            for row in table[1:]:
                # День недели (обычно 1-я колонка, index 0)
                day_cell = row[0]
                if day_cell:
                    raw_day = _fix_text_issues(day_cell).strip()
                    # Фильтр от мусора (иногда там цифры или пустые переносы)
                    if len(raw_day) > 3: 
                        current_day = raw_day.capitalize()
                
                # Время
                time_cell = row[time_col_idx]
                t_start, t_end = "", ""
                if time_cell:
                    tm = TIME_PATTERN.search(time_cell)
                    if tm:
                        t_start = tm.group(1).replace('.', ':')
                        t_end = tm.group(2).replace('.', ':')
                
                # Если в строке нет времени, мы не можем привязать пару. Пропускаем.
                if not t_start: continue

                # Проходим по всем колонкам, которые мы опознали как ГРУППЫ
                for col_idx, group_name in group_map.items():
                    if col_idx >= len(row): continue
                    
                    cell_text = row[col_idx]
                    
                    # --- ЛОГИКА "ОБЩИХ ПАР" (Merged Cells) ---
                    # Если ячейка группы пустая, проверяем колонки слева (от времени до текущей группы).
                    # Если там есть текст с "Лек" или "Общ", значит это пара на весь поток.
                    if not cell_text:
                        # Сканируем от (time_col + 1) до (col_idx)
                        for scan_idx in range(time_col_idx + 1, col_idx):
                            prev_text = row[scan_idx]
                            if prev_text:
                                pt_lower = prev_text.lower()
                                # Триггеры общей пары
                                if "лек" in pt_lower or "общ" in pt_lower or "поток" in pt_lower:
                                    cell_text = prev_text
                                    break
                    
                    if not cell_text or len(cell_text.strip()) < 3: continue
                    
                    # Чистим текст (перевертыши, переносы)
                    clean_text = _fix_text_issues(cell_text)

                    parsed_lesson = _parse_cell_text(clean_text)
                    parsed_lesson.time_start = t_start
                    parsed_lesson.time_end = t_end
                    
                    # Инициализируем список дней для группы, если нет
                    if current_day not in schedule_by_group[group_name]:
                        schedule_by_group[group_name][current_day] = []
                    
                    schedule_by_group[group_name][current_day].append(parsed_lesson)

    # --- 3. СБОРКА РЕЗУЛЬТАТА ---
    final_output = {}
    for gr_name, days_dict in schedule_by_group.items():
        week_list = []
        for day, lessons in days_dict.items():
            week_list.append(DaySchedule(day_name=day, lessons=lessons))
        final_output[gr_name] = week_list

    return ParsedScheduleResponse(groups=final_output)

def _fix_text_issues(text: str) -> str:
    """
    Лечит баги PDF:
    1. Перевернутый текст (киньледеноП -> Понедельник)
    2. Лишние переносы строк
    """
    if not text: return ""
    # Убираем переносы
    text = text.replace('\n', ' ').strip()
    
    # Детектор перевертыша:
    # Если строка длиннее 3 символов, заканчивается Заглавной, а начинается с маленькой
    # Пример: "киньледеноП" -> ends with 'П' (Upper), starts with 'к' (lower)
    if len(text) > 3 and text[-1].isupper() and text[0].islower():
        # Переворачиваем
        reversed_text = text[::-1]
        # Простая эвристика: если после переворота мы видим знакомые слова, значит это оно
        check_lower = reversed_text.lower()
        keywords = ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'лек', 'прак', 'англ']
        
        # Если хоть одно ключевое слово найдено - возвращаем перевертыш
        if any(k in check_lower for k in keywords):
            return reversed_text
        
        # Если ключевых слов нет, но паттерн очень похож (например фамилия препода), все равно пробуем
        return reversed_text

    return text

def _parse_cell_text(text: str) -> LessonItem:
    """Выкусывает метаданные из строки с помощью Regex"""
    text = text.strip()
    
    # 1. Тип занятия
    l_type = "Прак" # Дефолт
    tm = TYPE_PATTERN.search(text)
    if tm:
        found_type = tm.group(1).lower()
        if "лек" in found_type: l_type = "Лекция"
        elif "сем" in found_type: l_type = "Семинар"
        elif "лаб" in found_type: l_type = "Лаба"
        elif "экз" in found_type: l_type = "Экзамен"
        else: l_type = "Практика"
        
        text = text.replace(tm.group(0), "")

    # 2. Аудитория
    room = ""
    rm = ROOM_PATTERN.search(text)
    if rm:
        room = rm.group(1)
        text = text.replace(room, "")

    # 3. Преподаватель
    teacher = ""
    tcm = TEACHER_PATTERN.search(text)
    if tcm:
        teacher = tcm.group(1)
        text = text.replace(teacher, "")

    # 4. Предмет (всё остальное)
    # Удаляем лишние пробелы и знаки препинания по краям
    subject = re.sub(r'\s+', ' ', text).strip(" .,-")
    
    if len(subject) < 2: 
        subject = "Занятие"

    return LessonItem(
        subject=subject,
        type=l_type,
        teacher=teacher,
        room=room,
        time_start="", 
        time_end=""
    )