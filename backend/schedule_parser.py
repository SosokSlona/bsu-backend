import pdfplumber
import re
import io
from typing import List, Dict, Tuple
from collections import Counter
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к|ауд\.?)\b', re.IGNORECASE)
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?)\)', re.IGNORECASE)
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

# Ключевые слова для детектора колонок
DAYS_KEYWORDS = {'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье'}
IGNORE_HEADERS = {'день', 'дни', 'время', 'часы', 'час', '№', 'п/п', 'предмет', 'специальность', 'курс', 'группа'}

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    if course < 1: course = 1
    # Открываем только страницы курса
    start_page = (course - 1) * 2
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Безопасный выбор страниц
        pages = []
        for i in range(start_page, min(start_page + 2, len(pdf.pages))):
            pages.append(pdf.pages[i])
        if not pages: pages = pdf.pages # Fallback

        for page in pages:
            # 1. Извлекаем таблицу
            table = page.extract_table({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 5,
                "intersection_tolerance": 5
            })
            
            if not table or len(table) < 2: continue

            # 2. АНАЛИЗ СТРУКТУРЫ (Кто есть кто?)
            # Мы не верим, что 0-я колонка это день. Мы проверяем.
            day_col_idx, time_col_idx, group_col_indices = _analyze_column_roles(table)
            
            # Если не нашли время или группы - таблица мусорная, скипаем
            if time_col_idx is None or not group_col_indices:
                continue

            # Определяем названия групп из шапки (обычно первая строка, где есть текст в колонке группы)
            group_names_map = {}
            header_row = table[0]
            for idx in group_col_indices:
                # Пытаемся найти название группы в первых строках
                g_name = _clean_text(header_row[idx])
                # Если в первой строке пусто, ищем ниже (иногда шапка merged)
                if len(g_name) < 3:
                    for r_check in table[1:3]:
                        candidate = _clean_text(r_check[idx])
                        if len(candidate) > 2 and "группа" not in candidate.lower():
                            g_name = candidate
                            break
                
                # Если всё еще пусто или мусор - называем "Группа {idx}"
                if len(g_name) < 2 or g_name.lower() in IGNORE_HEADERS:
                    g_name = f"Группа {idx+1}"
                
                group_names_map[idx] = g_name
                if g_name not in schedule_by_group:
                    schedule_by_group[g_name] = {}

            # 3. ПАРСИНГ СТРОК
            current_day = "Понедельник" # Дефолт
            
            for row_idx, row in enumerate(table):
                # Пропускаем шапку
                if row_idx == 0: continue

                # А. Ищем День (ТОЛЬКО в колонке дня)
                if day_col_idx is not None:
                    day_cell = _clean_text(row[day_col_idx])
                    # Проверяем, что это реально день недели
                    if day_cell.lower() in DAYS_KEYWORDS:
                        current_day = day_cell.capitalize()

                # Б. Ищем Время (ТОЛЬКО в колонке времени)
                time_cell = _clean_text(row[time_col_idx])
                t_match = TIME_PATTERN.search(time_cell)
                if not t_match:
                    continue # Без времени строка бесполезна для расписания
                
                t_start = t_match.group(1).replace('.', ':')
                t_end = t_match.group(2).replace('.', ':')

                # В. Читаем ячейки Групп
                for g_idx, g_name in group_names_map.items():
                    if g_idx >= len(row): continue
                    
                    cell_text = row[g_idx]
                    
                    # Логика "Общей лекции" (Look Left)
                    # Если ячейка пуста, смотрим влево до колонки времени
                    if not cell_text:
                        for scan in range(time_col_idx + 1, g_idx):
                            neighbor = row[scan]
                            if neighbor and ("лек" in neighbor.lower() or "общ" in neighbor.lower()):
                                cell_text = neighbor
                                break
                    
                    clean_txt = _clean_text(cell_text)
                    if len(clean_txt) < 3: continue

                    # Парсим и разбиваем (Англ/Нем)
                    lessons = _parse_and_split_cell(clean_txt)
                    
                    if current_day not in schedule_by_group[g_name]:
                        schedule_by_group[g_name][current_day] = []
                    
                    for l in lessons:
                        l.time_start = t_start
                        l.time_end = t_end
                        schedule_by_group[g_name][current_day].append(l)

    # 4. Сборка ответа
    final_output = {}
    for g_name, days in schedule_by_group.items():
        week = []
        # Сортируем дни? Пока оставим как есть, обычно они идут по порядку
        for d_name, lessons in days.items():
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    return ParsedScheduleResponse(groups=final_output)

def _analyze_column_roles(table: List[List[str]]) -> Tuple[int, int, List[int]]:
    """
    Сканирует таблицу вертикально и определяет:
    - Где колонка Дней
    - Где колонка Времени
    - Где колонки Групп
    Возвращает: (day_idx, time_idx, [group_indices])
    """
    day_idx = None
    time_idx = None
    group_indices = []
    
    num_cols = len(table[0])
    
    # Пробегаем по каждой колонке (смотрим первые 20 строк)
    for col in range(num_cols):
        col_values = []
        for row in table[:25]: 
            if col < len(row) and row[col]:
                col_values.append(_clean_text(row[col]).lower())
        
        # Считаем совпадения
        days_score = sum(1 for v in col_values if v in DAYS_KEYWORDS)
        time_score = sum(1 for v in col_values if TIME_PATTERN.search(v))
        
        # Если много дней -> это колонка Дня (обычно самая левая)
        if days_score > 0 and day_idx is None:
            day_idx = col
            continue
            
        # Если много времени -> это колонка Времени
        if time_score > 1 and time_idx is None:
            time_idx = col
            continue
            
        # Иначе это кандидат на Группу (если не пустая и не заголовок)
        # Проверяем, что это не "Часы" и не "Дни"
        header_val = ""
        if len(col_values) > 0: header_val = col_values[0]
        
        if header_val not in IGNORE_HEADERS:
            group_indices.append(col)
            
    # Fallback: Если время не нашли, но нашли день, то время обычно следующее
    if time_idx is None and day_idx is not None:
        time_idx = day_idx + 1
        # Убираем time_idx из списка групп, если он туда попал
        if time_idx in group_indices: group_indices.remove(time_idx)

    return day_idx, time_idx, group_indices

def _clean_text(text: str) -> str:
    """Чистит мусор и лечит перевернутый текст"""
    if not text: return ""
    text = text.replace('\n', ' ').strip()
    
    # Лечим "киньледеноП"
    if len(text) > 3 and text[-1].isupper() and text[0].islower():
        rev = text[::-1]
        check = rev.lower()
        if any(k in check for k in list(DAYS_KEYWORDS) + ['лек', 'прак', 'англ']):
            return rev
    return text

def _parse_and_split_cell(text: str) -> List[LessonItem]:
    """Разбивает ячейку с несколькими предметами (Иняз)"""
    text_lower = text.lower()
    is_complex = "язык" in text_lower or "спецмодуль" in text_lower or "физ" in text_lower
    
    if not is_complex:
        return [_extract_single_lesson(text)]
        
    teachers = list(TEACHER_PATTERN.finditer(text))
    if len(teachers) <= 1:
        return [_extract_single_lesson(text)]
        
    results = []
    base_prefix = text[:teachers[0].start()].strip()
    base_prefix = re.sub(r'\d', '', base_prefix).strip(" .(-")
    if len(base_prefix) < 3: base_prefix = "Иностр. язык"

    for i, match in enumerate(teachers):
        t_name = match.group(1)
        start = match.start()
        end = teachers[i+1].start() if i < len(teachers) - 1 else len(text)
        prev_end = teachers[i-1].end() if i > 0 else 0
        
        chunk = text[prev_end:end].strip()
        
        # Аудитория
        room = ""
        rm = ROOM_PATTERN.search(chunk)
        if rm: room = rm.group(1)
        
        # Тип
        l_type = "Прак"
        tm = TYPE_PATTERN.search(chunk)
        if tm: l_type = tm.group(1).capitalize()
        
        # Подгруппа
        subgroup = f"Группа {i+1}"
        cl = chunk.lower()
        if "англ" in cl: subgroup = "Английский"
        elif "нем" in cl: subgroup = "Немецкий"
        elif "фран" in cl: subgroup = "Французский"
        elif "исп" in cl: subgroup = "Испанский"
        elif "кит" in cl: subgroup = "Китайский"

        results.append(LessonItem(
            subject=f"{base_prefix} ({subgroup})",
            type=l_type,
            teacher=t_name,
            room=room,
            time_start="", time_end="",
            subgroup=subgroup
        ))
    return results

def _extract_single_lesson(text: str) -> LessonItem:
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        val = tm.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "экз" in val: l_type = "Экзамен"
        text = text.replace(tm.group(0), "")

    room = ""
    rm = ROOM_PATTERN.search(text)
    if rm:
        room = rm.group(1)
        text = text.replace(room, "")

    teacher = ""
    tcm = TEACHER_PATTERN.search(text)
    if tcm:
        teacher = tcm.group(1)
        text = text.replace(teacher, "")

    subject = re.sub(r'\s+', ' ', text).strip(" .,-")
    if len(subject) < 2: subject = "Занятие"

    return LessonItem(subject=subject, type=l_type, teacher=teacher, room=room, time_start="", time_end="", subgroup=None)