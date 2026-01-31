import pdfplumber
import re
import io
from typing import List, Dict, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# Регулярки для очистки и поиска
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф)\)', re.IGNORECASE)

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"🚀 [PLUMBER] Starting parsing... Size: {len(pdf_bytes)}")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    current_day = "Понедельник" # Дефолт
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Выбираем страницы (для 2 курса это обычно 3 и 4, но берем диапазон)
        start_page = max(0, (course - 1) * 2)
        pages_to_parse = pdf.pages[start_page : start_page + 2]
        
        for page in pages_to_parse:
            # Ищем таблицы
            tables = page.extract_tables()
            
            for table in tables:
                # 1. Анализ шапки (первая строка)
                if not table or len(table) < 2: continue
                
                header = table[0]
                day_col_idx = -1
                time_col_idx = -1
                group_map = {} # {index: "Group Name"}

                # Ищем индексы колонок
                for idx, cell in enumerate(header):
                    if not cell: continue
                    txt = cell.lower().replace('\n', ' ')
                    if 'дни' in txt or 'день' in txt: day_col_idx = idx
                    elif 'часы' in txt or 'время' in txt: time_col_idx = idx
                    elif 'группа' in txt or 'специальность' not in txt:
                        # Чистим название группы
                        g_name = cell.replace('\n', ' ').strip()
                        if len(g_name) > 2:
                            group_map[idx] = g_name

                # Если не нашли время, пробуем 2-ю колонку
                if time_col_idx == -1 and len(header) > 1: time_col_idx = 1
                
                # Если групп не нашли в шапке, берем все колонки правее времени
                if not group_map:
                    start_g = (time_col_idx + 1) if time_col_idx != -1 else 2
                    for i in range(start_g, len(header)):
                        group_map[i] = f"Группа {i}" # Временное название, если в шапке пусто

                # 2. Парсинг строк
                last_time = ""
                
                for row in table[1:]: # Пропускаем шапку
                    # Обработка ДНЯ (Merged Cells)
                    if day_col_idx != -1:
                        d_val = row[day_col_idx]
                        if d_val and len(d_val.strip()) > 2:
                            d_clean = d_val.replace('\n', '').strip().capitalize()
                            if any(d in d_clean.lower() for d in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']):
                                current_day = d_clean
                    
                    # Обработка ВРЕМЕНИ
                    t_val = row[time_col_idx] if time_col_idx != -1 else None
                    if t_val:
                        t_val = t_val.replace('\n', '').strip()
                        if TIME_PATTERN.search(t_val):
                            last_time = t_val
                    
                    if not last_time: continue # Без времени скипаем
                    
                    # Разбор времени
                    tm = TIME_PATTERN.search(last_time)
                    t_start, t_end = tm.group(1).replace('.', ':'), tm.group(2).replace('.', ':') if tm else ("", "")

                    # Обработка ГРУПП
                    for g_idx, g_name in group_map.items():
                        if g_idx >= len(row): continue
                        
                        cell_text = row[g_idx]
                        if not cell_text or len(cell_text.strip()) < 3: continue
                        
                        # Парсим содержимое ячейки
                        items = _parse_cell_text(cell_text)
                        
                        if g_name not in schedule_by_group: schedule_by_group[g_name] = {}
                        if current_day not in schedule_by_group[g_name]: schedule_by_group[g_name][current_day] = []
                        
                        for item in items:
                            item.time_start = t_start
                            item.time_end = t_end
                            schedule_by_group[g_name][current_day].append(item)

    # Формируем ответ
    final_output = {}
    for g_name, days in schedule_by_group.items():
        week = []
        # Сортируем дни
        day_order = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
        sorted_days = sorted(days.items(), key=lambda x: day_order.index(x[0]) if x[0] in day_order else 9)
        
        for d_name, lessons in sorted_days:
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    print(f"✅ [PLUMBER] Done. Found groups: {list(final_output.keys())}")
    return ParsedScheduleResponse(groups=final_output)

def _parse_cell_text(text: str) -> List[LessonItem]:
    text = text.replace('\n', ' ').strip()
    # Разделяем, если в ячейке несколько предметов (обычно разделены преподавателями)
    teachers_matches = list(TEACHER_PATTERN.finditer(text))
    
    if not teachers_matches:
        return [_create_item(text, "")]
        
    results = []
    prev_end = 0
    
    # Если текст начинается не с препода, это название предмета
    base_subject = text[:teachers_matches[0].start()].strip()
    
    for i, match in enumerate(teachers_matches):
        teacher = match.group(0)
        start = match.start()
        end = match.end()
        
        # Текст после препода (обычно аудитория)
        next_start = teachers_matches[i+1].start() if i + 1 < len(teachers_matches) else len(text)
        details = text[end:next_start]
        
        # Формируем название. Если есть общий заголовок ячейки, используем его
        subj = base_subject if i == 0 and len(base_subject) > 2 else "Занятие"
        if len(base_subject) < 3: # Если общего нет, ищем в куске перед преподом
             local_chunk = text[prev_end:start].strip()
             if len(local_chunk) > 2: subj = local_chunk
        
        full_text_chunk = subj + " " + details
        item = _create_item(full_text_chunk, teacher)
        
        # Подгруппы
        lower_txt = (subj + details).lower()
        if "1" in lower_txt and "группа" not in lower_txt: item.subgroup = "Подгруппа 1"
        if "2" in lower_txt and "группа" not in lower_txt: item.subgroup = "Подгруппа 2"
        if "англ" in lower_txt: item.subgroup = "Английский"
        if "нем" in lower_txt: item.subgroup = "Немецкий"
        
        results.append(item)
        prev_end = next_start
        
    return results

def _create_item(text, teacher):
    # Тип
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        t_val = tm.group(1).lower()
        if "лек" in t_val: l_type = "Лекция"
        elif "сем" in t_val: l_type = "Семинар"
        elif "лаб" in t_val: l_type = "Лаба"
        text = text.replace(tm.group(0), "")
        
    # Аудитория (3-4 цифры)
    room = ""
    rm = re.search(r'\b\d{3,4}[а-я]?\b', text)
    if rm:
        room = rm.group(0)
        text = text.replace(room, "")
    
    subj = text.strip(" .,-")
    if len(subj) < 2: subj = "Занятие"
    
    return LessonItem(subject=subj, type=l_type, teacher=teacher.strip(), room=room, time_start="", time_end="")