import pdfplumber
import re
import io
from typing import List, Dict, Tuple, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к|ауд\.?)\b', re.IGNORECASE)
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?)\)', re.IGNORECASE)
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

# СЛОВАРЬ ДЛЯ ДЕШИФРОВКИ (чтобы узнавать перевернутые слова)
VOCABULARY = {
    'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье',
    'время', 'день', 'лекция', 'семинар', 'практика', 'англ', 'нем', 'исп', 'фран',
    'язык', 'группа', 'курс', 'специальность', 'иностранный'
}

# Слова-паразиты для названий групп
IGNORE_HEADERS = {'день', 'дни', 'время', 'часы', 'час', '№', 'п/п', 'предмет', 'специальность', 'курс', 'группа'}

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    if course < 1: course = 1
    start_page = (course - 1) * 2
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = []
        # Берем страницы с запасом, если вдруг верстка съехала
        max_idx = min(start_page + 2, len(pdf.pages))
        for i in range(start_page, max_idx):
            pages.append(pdf.pages[i])
        if not pages: pages = pdf.pages

        for page in pages:
            # layout=True помогает при разреженном тексте (к и н ь...)
            table = page.extract_table({
                "vertical_strategy": "lines",
                "horizontal_strategy": "lines",
                "snap_tolerance": 4,
                "intersection_tolerance": 5,
            })
            
            if not table or len(table) < 2: continue

            # 1. АНАЛИЗ КОЛОНОК (Сначала лечим текст, потом определяем роль)
            day_col, time_col, group_cols = _analyze_column_roles(table)
            
            if time_col is None or not group_cols: continue

            # 2. ОПРЕДЕЛЯЕМ НАЗВАНИЯ ГРУПП
            group_names_map = {}
            # Ищем название в первых 3 строках
            for g_idx in group_cols:
                g_name = ""
                for r_idx in range(3): 
                    if r_idx < len(table):
                        candidate = _aggressive_fix_text(table[r_idx][g_idx])
                        if len(candidate) > 2 and candidate.lower() not in IGNORE_HEADERS:
                            g_name = candidate
                            break
                
                # Если название пустое или похоже на "День" (из-за ошибки), даем дефолт
                if not g_name or g_name.lower() in VOCABULARY:
                    g_name = f"Группа {g_idx+1}"
                
                group_names_map[g_idx] = g_name
                if g_name not in schedule_by_group:
                    schedule_by_group[g_name] = {}

            # 3. ПАРСИНГ СТРОК
            current_day = "Понедельник"
            
            for row in table[1:]:
                # А. День
                if day_col is not None:
                    d_raw = _aggressive_fix_text(row[day_col])
                    if d_raw.lower() in VOCABULARY and len(d_raw) > 3:
                        current_day = d_raw.capitalize()

                # Б. Время
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
                            neighbor = row[scan]
                            if neighbor:
                                n_fixed = _aggressive_fix_text(neighbor)
                                if "лек" in n_fixed.lower() or "общ" in n_fixed.lower():
                                    cell_text = neighbor # Берем оригинал, починим внутри
                                    break
                    
                    fixed_text = _aggressive_fix_text(cell_text)
                    if len(fixed_text) < 3: continue

                    # Разбиваем на подгруппы
                    lessons = _parse_and_split_cell(fixed_text)
                    
                    if current_day not in schedule_by_group[g_name]:
                        schedule_by_group[g_name][current_day] = []
                    
                    for l in lessons:
                        l.time_start = t_start
                        l.time_end = t_end
                        schedule_by_group[g_name][current_day].append(l)

    # 4. СБОРКА
    final_output = {}
    for g_name, days in schedule_by_group.items():
        # Фильтруем пустые дни
        if not days: continue
        week = []
        for d_name, lessons in days.items():
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    return ParsedScheduleResponse(groups=final_output)

def _aggressive_fix_text(text: Optional[str]) -> str:
    """
    Мощный дешифратор текста.
    Чинит: "к и н ь л е д е н о П" -> "Понедельник"
    Чинит: "г р е в т е Ч" -> "Четверг"
    """
    if not text: return ""
    # 1. Базовая чистка
    clean = text.replace('\n', ' ').strip()
    if not clean: return ""

    # 2. Если текст читается нормально - возвращаем
    if any(word in clean.lower() for word in ['понедельник', 'лекция', 'язык', 'группа']):
        return clean

    # 3. Пробуем убрать пробелы и перевернуть
    # "к и н ь л е д е н о П" -> "киньледеноП" -> "Понедельник"
    no_spaces = clean.replace(' ', '')
    reversed_text = no_spaces[::-1]
    
    # Проверяем по словарю
    reversed_lower = reversed_text.lower()
    for word in VOCABULARY:
        if word in reversed_lower:
            # Если нашли совпадение, значит это был перевертыш
            # Но нам нужно вернуть красивый регистр
            # Если это день недели, возвращаем с большой буквы
            if word in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                return word.capitalize()
            return reversed_text # Иначе возвращаем как есть (например "Англ")

    # 4. Если это не словарь, но похоже на перевертыш (Начинается с маленькой, кончается Большой)
    if len(no_spaces) > 3 and no_spaces[-1].isupper() and no_spaces[0].islower():
        return reversed_text

    return clean

def _analyze_column_roles(table: List[List[str]]) -> Tuple[Optional[int], Optional[int], List[int]]:
    """Определяет роли колонок, предварительно исправляя текст"""
    day_idx = None
    time_idx = None
    group_indices = []
    
    if not table: return None, None, []
    num_cols = len(table[0])
    
    for col in range(num_cols):
        col_content = []
        # Смотрим первые 30 строк
        for row in table[:30]:
            if col < len(row):
                # ВАЖНО: Лечим текст перед анализом!
                txt = _aggressive_fix_text(row[col]).lower()
                if txt: col_content.append(txt)
        
        # Анализируем содержимое
        days_score = sum(1 for x in col_content if x in VOCABULARY and x in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'])
        time_score = sum(1 for x in col_content if TIME_PATTERN.search(x))
        
        if days_score >= 1 and day_idx is None:
            day_idx = col
        elif time_score >= 2 and time_idx is None:
            time_idx = col
        else:
            # Проверяем, не заголовок ли это
            header_candidate = col_content[0] if col_content else ""
            if header_candidate not in IGNORE_HEADERS:
                group_indices.append(col)
    
    # Корректировка: Если "Четверг" попал в группы, убираем его
    if day_idx in group_indices: group_indices.remove(day_idx)
    if time_idx in group_indices: group_indices.remove(time_idx)
    
    return day_idx, time_idx, group_indices

def _clean_subject_garbage(text: str) -> str:
    """Удаляет огрызки типа '- . ,' после вырезания преподов"""
    # Убираем множественные пробелы
    text = re.sub(r'\s+', ' ', text)
    # Убираем знаки препинания в начале и конце
    text = text.strip(" .,-–/\\|")
    # Убираем висячие скобки "()"
    text = text.replace("()", "")
    # Убираем "(ф)" (частый мусор от "факультатив" или формы обучения)
    text = text.replace("(ф)", "")
    return text.strip()

def _parse_and_split_cell(text: str) -> List[LessonItem]:
    text_lower = text.lower()
    # Триггеры для сложного парсинга
    is_complex = "язык" in text_lower or "спецмодуль" in text_lower or "физ" in text_lower
    
    if not is_complex:
        return [_extract_single_lesson(text)]
        
    teachers = list(TEACHER_PATTERN.finditer(text))
    if len(teachers) <= 1:
        return [_extract_single_lesson(text)]
        
    results = []
    # Базовое название (до первого препода)
    base_prefix = text[:teachers[0].start()]
    base_prefix = _clean_subject_garbage(base_prefix)
    if len(base_prefix) < 3: base_prefix = "Иностр. язык"

    for i, match in enumerate(teachers):
        t_name = match.group(1)
        start = match.start()
        end = teachers[i+1].start() if i < len(teachers) - 1 else len(text)
        prev_end = teachers[i-1].end() if i > 0 else 0
        
        chunk = text[prev_end:end]
        
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
            subject=f"{base_prefix}", # ({subgroup}) убрал из названия, оно есть в subgroup
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

    # Финальная чистка
    subject = _clean_subject_garbage(text)
    if len(subject) < 2: subject = "Занятие"

    return LessonItem(subject=subject, type=l_type, teacher=teacher, room=room, time_start="", time_end="", subgroup=None)