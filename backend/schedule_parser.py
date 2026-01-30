import pdfplumber
import re
import io
from typing import List, Dict, Tuple, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к|ауд\.?)\b', re.IGNORECASE)
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф)\)', re.IGNORECASE)
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

# Словарь для восстановления перевернутого текста
VOCABULARY = {
    'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье',
    'время', 'день', 'часы'
}

IGNORED_HEADERS = {'день', 'дни', 'время', 'часы', 'час', '№', 'п/п', 'предмет'}

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
            # 1. ОБРЕЗАЕМ МУСОР СВЕРХУ (Специальность, Утверждаю и т.д.)
            cropped_page = _crop_header(page)
            if not cropped_page: continue

            # 2. ИЗВЛЕКАЕМ ТАБЛИЦУ
            # Используем настройки для максимальной точности линий
            tables = cropped_page.extract_table({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "intersection_y_tolerance": 5,
                "snap_tolerance": 4
            })
            
            if not tables or len(tables) < 2: continue

            # 3. ОПРЕДЕЛЯЕМ РОЛИ КОЛОНОК
            # Нам нужно понять, где День, где Время, а где Группы
            day_col, time_col, group_cols = _analyze_columns(tables)
            
            if time_col is None or not group_cols: continue

            # 4. ИЩЕМ НАЗВАНИЯ ГРУПП (В шапке)
            group_names = {}
            header_row = tables[0]
            
            # Пробуем найти названия в первой строке
            for idx in group_cols:
                raw_name = _fix_text(header_row[idx])
                # Если название короткое или пустое, пробуем искать в тексте выше (в оригинале)
                # Но для простоты, если пусто, даем имя "Группа X"
                if len(raw_name) < 2 or raw_name.lower() in IGNORED_HEADERS:
                    # Попытка вытянуть название из content (иногда оно merged)
                    raw_name = f"Группа {idx}" 
                
                # Очистка от "Группа" если есть номер
                if "группа" in raw_name.lower() and len(raw_name) > 10:
                     raw_name = raw_name.replace('\n', ' ').strip()

                group_names[idx] = raw_name.strip()
                if group_names[idx] not in schedule_by_group:
                    schedule_by_group[group_names[idx]] = {}

            # 5. ПАРСИМ СТРОКИ
            current_day = "Понедельник"
            
            for row in tables[1:]:
                # А. День
                if day_col is not None and day_col < len(row):
                    d_txt = _fix_text(row[day_col])
                    if d_txt.lower() in VOCABULARY:
                        current_day = d_txt.capitalize()

                # Б. Время
                if time_col >= len(row): continue
                t_txt = _fix_text(row[time_col])
                t_match = TIME_PATTERN.search(t_txt)
                if not t_match: continue # Строка без времени — мусор
                
                t_start = t_match.group(1).replace('.', ':')
                t_end = t_match.group(2).replace('.', ':')

                # В. Группы
                for g_idx in group_cols:
                    if g_idx >= len(row): continue
                    
                    cell_text = row[g_idx]
                    
                    # --- ЛОГИКА ОБЩИХ ПАР (LOOK LEFT) ---
                    # Если ячейка пустая, но слева (после времени) есть текст с "Лек" или "Общ"
                    if not cell_text:
                        for scan in range(time_col + 1, g_idx):
                            if scan >= len(row): break
                            neighbor = row[scan]
                            if neighbor:
                                n_clean = _fix_text(neighbor).lower()
                                if "лек" in n_clean or "общ" in n_clean or "поток" in n_clean:
                                    cell_text = neighbor
                                    break
                    
                    clean_cell = _fix_text(cell_text)
                    if len(clean_cell) < 3: continue

                    # Парсим содержимое (может быть несколько предметов)
                    lessons = _parse_cell(clean_cell)
                    
                    g_name = group_names[g_idx]
                    if current_day not in schedule_by_group[g_name]:
                        schedule_by_group[g_name][current_day] = []
                        
                    for l in lessons:
                        l.time_start = t_start
                        l.time_end = t_end
                        schedule_by_group[g_name][current_day].append(l)

    # 6. ФИНАЛЬНАЯ СБОРКА
    final_output = {}
    for g_name, days in schedule_by_group.items():
        if not days: continue
        week = []
        for d_name, lessons in days.items():
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    return ParsedScheduleResponse(groups=final_output)

def _crop_header(page):
    """Отрезает шапку страницы, находя первую горизонтальную линию таблицы"""
    # Ищем линии
    lines = page.lines
    if not lines: return page
    
    # Сортируем горизонтальные линии по Y (top)
    h_lines = sorted([l for l in lines if l['width'] > 100], key=lambda x: x['top'])
    
    if not h_lines: return page
    
    # Берем первую линию как начало таблицы
    top_y = h_lines[0]['top']
    
    # Отрезаем всё что выше (минус пару пикселей для надежности)
    # Формат crop: (x0, top, x1, bottom)
    return page.crop((0, top_y - 2, page.width, page.height))

def _analyze_columns(table: List[List[str]]) -> Tuple[Optional[int], Optional[int], List[int]]:
    """Определяет индексы колонок"""
    day_idx = None
    time_idx = None
    group_indices = []
    
    if not table: return None, None, []
    
    # Сканируем первые 20 строк
    num_cols = len(table[0])
    
    for col in range(num_cols):
        col_text = []
        for row in table[:20]:
            if col < len(row) and row[col]:
                col_text.append(_fix_text(row[col]).lower())
        
        # Очки рейтинга
        is_day = any(d in col_text for d in ['понедельник', 'вторник', 'среда'])
        is_time = any(TIME_PATTERN.search(t) for t in col_text)
        
        if is_day and day_idx is None:
            day_idx = col
        elif is_time and time_idx is None:
            time_idx = col
        else:
            # Кандидат в группу: не пустая и заголовок не из стоп-листа
            header = col_text[0] if col_text else ""
            if header not in IGNORED_HEADERS:
                group_indices.append(col)
                
    # Коррекция: Если время попало в группы
    if time_idx in group_indices: group_indices.remove(time_idx)
    if day_idx in group_indices: group_indices.remove(day_idx)
    
    return day_idx, time_idx, group_indices

def _fix_text(text: Optional[str]) -> str:
    """Лечит перевертыши и мусор"""
    if not text: return ""
    clean = text.replace('\n', ' ').strip()
    if not clean: return ""

    # Проверка на перевернутый день недели "к и н ь л е д..."
    # Удаляем пробелы для проверки
    no_spaces = clean.replace(' ', '')
    reversed_clean = no_spaces[::-1].lower()
    
    for word in VOCABULARY:
        if word in reversed_clean:
            # Нашли! Возвращаем нормальное слово
            if word in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                return word.capitalize()
            return word # Иначе просто слово (например "время")

    # Если текст нормальный, но с лишними переносами
    return re.sub(r'\s+', ' ', clean)

def _parse_cell(text: str) -> List[LessonItem]:
    """Разбивает ячейку на предметы"""
    # 1. Очистка от мусора типа "ф - ."
    text = text.strip(" .-–")
    if len(text) < 3: return []

    # 2. Проверка на сложный предмет (Иностранный)
    is_complex = "язык" in text.lower() or "спецмодуль" in text.lower()
    
    teachers = list(TEACHER_PATTERN.finditer(text))
    
    if not is_complex or len(teachers) <= 1:
        return [_extract_lesson_data(text)]
        
    # Разделение по преподавателям
    results = []
    base_name = text[:teachers[0].start()].strip(" .-")
    if len(base_name) < 3: base_name = "Иностр. язык"

    for i, match in enumerate(teachers):
        t_name = match.group(1)
        end = teachers[i+1].start() if i < len(teachers) - 1 else len(text)
        prev_end = teachers[i-1].end() if i > 0 else 0
        chunk = text[prev_end:end]
        
        # Вытаскиваем детали из куска
        parsed = _extract_lesson_data(chunk)
        
        # Формируем подгруппу
        subgroup = f"Группа {i+1}"
        cl = chunk.lower()
        if "англ" in cl: subgroup = "Английский"
        elif "нем" in cl: subgroup = "Немецкий"
        elif "фран" in cl: subgroup = "Французский"
        elif "исп" in cl: subgroup = "Испанский"
        elif "кит" in cl: subgroup = "Китайский"

        parsed.subject = base_name
        parsed.teacher = t_name # Уточняем препода
        parsed.subgroup = subgroup
        
        results.append(parsed)
        
    return results

def _extract_lesson_data(text: str) -> LessonItem:
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        val = tm.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "экз" in val: l_type = "Экзамен"
        elif "ф" in val: l_type = "Факультатив"
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

    subject = re.sub(r'\s+', ' ', text).strip(" .,-–")
    if len(subject) < 2: subject = "Занятие"

    return LessonItem(subject=subject, type=l_type, teacher=teacher, room=room, time_start="", time_end="", subgroup=None)