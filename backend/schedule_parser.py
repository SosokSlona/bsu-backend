import pdfplumber
import re
import io
from typing import List, Dict, Tuple, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к|ауд\.?)\b', re.IGNORECASE)
# Добавили (ф) в типы
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф)\)', re.IGNORECASE)
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

# СЛОВАРЬ ДЛЯ ДЕШИФРОВКИ
VOCABULARY = {
    'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье',
    'время', 'день', 'лекция', 'семинар', 'практика', 'англ', 'нем', 'исп', 'фран',
    'язык', 'группа', 'курс', 'специальность', 'иностранный'
}

# Стоп-слова для колонок
IGNORE_HEADERS = {'день', 'дни', 'время', 'часы', 'час', '№', 'п/п', 'предмет', 'специальность', 'курс', 'код', 'название'}

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    if course < 1: course = 1
    start_page = (course - 1) * 2
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = []
        max_idx = min(start_page + 2, len(pdf.pages))
        for i in range(start_page, max_idx):
            pages.append(pdf.pages[i])
        if not pages: pages = pdf.pages

        for page in pages:
            # 1. ОБРЕЗАНИЕ ШАПКИ (CROP)
            # Отрезаем верхние 12% страницы, где обычно написана специальность и коды
            width = page.width
            height = page.height
            # Оставляем область: (x0, top=12%, x1, bottom)
            cropped_page = page.crop((0, height * 0.12, width, height))
            
            # Извлекаем таблицу из обрезанной страницы
            table = cropped_page.extract_table({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 4,
                "intersection_tolerance": 5,
            })
            
            if not table or len(table) < 2: continue

            # 2. АНАЛИЗ КОЛОНОК
            day_col, time_col, group_cols = _analyze_column_roles(table)
            
            if time_col is None or not group_cols: continue

            # 3. ОПРЕДЕЛЕНИЕ НАЗВАНИЙ ГРУПП
            group_names_map = {}
            for g_idx in group_cols:
                # Проверяем, есть ли вообще данные в этой колонке (защита от фантомов)
                if not _is_valid_group_column(table, g_idx):
                    continue

                g_name = ""
                # Ищем имя в первых строках таблицы
                for r_idx in range(min(3, len(table))): 
                    val = _aggressive_fix_text(table[r_idx][g_idx])
                    if len(val) > 2 and val.lower() not in IGNORE_HEADERS:
                        # Фильтр: если имя похоже на "6-05-..." (код специальности), пропускаем
                        if re.match(r'\d-\d+', val): continue
                        g_name = val
                        break
                
                # Дефолтное имя
                if not g_name or g_name.lower() in VOCABULARY:
                    g_name = f"Группа {g_idx}" # Используем индекс как временное имя
                
                group_names_map[g_idx] = g_name
                if g_name not in schedule_by_group:
                    schedule_by_group[g_name] = {}

            # 4. ПАРСИНГ СТРОК
            current_day = "Понедельник"
            
            for row in table:
                # Пропускаем совсем пустые строки
                if not any(row): continue

                # А. День
                if day_col is not None and day_col < len(row):
                    d_raw = _aggressive_fix_text(row[day_col])
                    if d_raw.lower() in VOCABULARY and len(d_raw) > 3:
                        current_day = d_raw.capitalize()

                # Б. Время
                if time_col >= len(row): continue
                t_raw = _aggressive_fix_text(row[time_col])
                t_match = TIME_PATTERN.search(t_raw)
                if not t_match: continue
                
                t_start = t_match.group(1).replace('.', ':')
                t_end = t_match.group(2).replace('.', ':')

                # В. Группы
                for g_idx, g_name in group_names_map.items():
                    if g_idx >= len(row): continue
                    
                    cell_text = row[g_idx]
                    
                    # Логика "Look Left" (Общая лекция)
                    if not cell_text:
                        for scan in range(time_col + 1, g_idx):
                            if scan >= len(row): break
                            neighbor = row[scan]
                            if neighbor:
                                n_fixed = _aggressive_fix_text(neighbor)
                                if "лек" in n_fixed.lower() or "общ" in n_fixed.lower():
                                    cell_text = neighbor
                                    break
                    
                    fixed_text = _aggressive_fix_text(cell_text)
                    if len(fixed_text) < 3: continue

                    # Парсинг
                    lessons = _parse_and_split_cell(fixed_text)
                    
                    if current_day not in schedule_by_group[g_name]:
                        schedule_by_group[g_name][current_day] = []
                    
                    for l in lessons:
                        l.time_start = t_start
                        l.time_end = t_end
                        schedule_by_group[g_name][current_day].append(l)

    # 5. СБОРКА И ОЧИСТКА ПУСТЫХ ГРУПП
    final_output = {}
    for g_name, days in schedule_by_group.items():
        # Если в группе нет уроков - не отдаем её
        if not days: continue
        
        # Если имя группы всё ещё "Группа X", пробуем переименовать её в нормальный порядковый номер
        final_name = g_name
        if g_name.startswith("Группа "):
             # Это просто заглушка, оставим как есть, или можно сгенерировать красивее
             pass

        week = []
        for d_name, lessons in days.items():
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[final_name] = week

    return ParsedScheduleResponse(groups=final_output)

def _is_valid_group_column(table, col_idx):
    """Проверяет, есть ли в колонке полезная нагрузка (не пустая ли она)"""
    content_count = 0
    total_checked = 0
    for row in table[1:]: # Пропускаем шапку
        if col_idx < len(row):
            txt = row[col_idx]
            if txt and len(txt.strip()) > 3:
                content_count += 1
        total_checked += 1
    
    # Если за всю страницу меньше 2 ячеек с текстом - это фантомная колонка
    if content_count < 2: return False
    return True

def _aggressive_fix_text(text: Optional[str]) -> str:
    if not text: return ""
    clean = text.replace('\n', ' ').strip()
    if not clean: return ""

    # Дешифратор перевертышей
    if any(word in clean.lower() for word in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']):
        return clean.capitalize()

    no_spaces = clean.replace(' ', '')
    if len(no_spaces) > 3 and no_spaces[-1].isupper() and no_spaces[0].islower():
        reversed_text = no_spaces[::-1]
        rev_lower = reversed_text.lower()
        for w in VOCABULARY:
            if w in rev_lower:
                if w in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                    return w.capitalize()
                return reversed_text
        return reversed_text # Рискнем вернуть перевернутое, если похоже на структуру

    return clean

def _analyze_column_roles(table: List[List[str]]) -> Tuple[Optional[int], Optional[int], List[int]]:
    day_idx = None
    time_idx = None
    group_indices = []
    
    if not table: return None, None, []
    num_cols = len(table[0])
    
    for col in range(num_cols):
        col_content = []
        # Сканируем всю таблицу
        for row in table:
            if col < len(row):
                txt = _aggressive_fix_text(row[col]).lower()
                if txt: col_content.append(txt)
        
        days_score = sum(1 for x in col_content if x in VOCABULARY and x in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'])
        time_score = sum(1 for x in col_content if TIME_PATTERN.search(x))
        
        if days_score >= 1 and day_idx is None:
            day_idx = col
        elif time_score >= 2 and time_idx is None:
            time_idx = col
        else:
            # Кандидат в группы. Проверяем, что это не мусор
            header = col_content[0] if col_content else ""
            if header not in IGNORE_HEADERS:
                group_indices.append(col)
    
    if day_idx in group_indices: group_indices.remove(day_idx)
    if time_idx in group_indices: group_indices.remove(time_idx)
    
    return day_idx, time_idx, group_indices

def _clean_subject_garbage(text: str) -> str:
    """Удаляет огрызки типа '- . ,'"""
    # Удаляем множественные пробелы
    text = re.sub(r'\s+', ' ', text)
    # Удаляем тире и точки В НАЧАЛЕ и КОНЦЕ
    # Также удаляем типичные разделители внутри, если они висят
    text = text.strip(" .,-–/\\|")
    text = text.replace("()", "")
    return text.strip()

def _extract_single_lesson(text: str) -> LessonItem:
    # 1. Тип
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        val = tm.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "экз" in val: l_type = "Экзамен"
        elif "ф" in val: l_type = "Факультатив" # Новое!
        text = text.replace(tm.group(0), "")

    # 2. Аудитория (ищем в конце или отдельно стоящую)
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

    # 4. Предмет (Чистим мусор)
    subject = _clean_subject_garbage(text)
    if len(subject) < 2: subject = "Занятие"

    return LessonItem(subject=subject, type=l_type, teacher=teacher, room=room, time_start="", time_end="", subgroup=None)

def _parse_and_split_cell(text: str) -> List[LessonItem]:
    text_lower = text.lower()
    is_complex = "язык" in text_lower or "спецмодуль" in text_lower or "физ" in text_lower
    
    if not is_complex:
        return [_extract_single_lesson(text)]
        
    teachers = list(TEACHER_PATTERN.finditer(text))
    if len(teachers) <= 1:
        return [_extract_single_lesson(text)]
        
    results = []
    base_prefix = text[:teachers[0].start()]
    base_prefix = _clean_subject_garbage(base_prefix)
    if len(base_prefix) < 3: base_prefix = "Иностр. язык"

    for i, match in enumerate(teachers):
        t_name = match.group(1)
        start = match.start()
        end = teachers[i+1].start() if i < len(teachers) - 1 else len(text)
        prev_end = teachers[i-1].end() if i > 0 else 0
        
        chunk = text[prev_end:end]
        
        room = ""
        rm = ROOM_PATTERN.search(chunk)
        if rm: room = rm.group(1)
        
        l_type = "Прак"
        tm = TYPE_PATTERN.search(chunk)
        if tm: l_type = tm.group(1).capitalize()
        
        subgroup = f"Группа {i+1}"
        cl = chunk.lower()
        if "англ" in cl: subgroup = "Английский"
        elif "нем" in cl: subgroup = "Немецкий"
        elif "фран" in cl: subgroup = "Французский"
        elif "исп" in cl: subgroup = "Испанский"

        results.append(LessonItem(
            subject=base_prefix,
            type=l_type,
            teacher=t_name,
            room=room,
            time_start="", time_end="",
            subgroup=subgroup
        ))
    return results