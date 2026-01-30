import pdfplumber
import re
import io
from typing import List, Dict
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
# Время: 8.30-9.50 или 08:30 - 09:50
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
# Аудитория: 3-4 цифры, возможно с буквой (501, 302а, с/з)
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к)\b', re.IGNORECASE)
# Тип занятия: (лек), (пр), (сем)
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?)\)', re.IGNORECASE)
# Преподаватель: Фамилия И.О.
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

def parse_schedule_pdf(pdf_bytes: bytes) -> ParsedScheduleResponse:
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            # Самая мощная функция: извлекает структуру таблицы
            # settings можно тюнить, если линии бледные, но дефолт обычно ок
            table = page.extract_table()
            
            if not table or len(table) < 2:
                continue

            # 1. Разбираем шапку (строка 0)
            headers = table[0] # ["День", "Время", "Таможенное дело (гр 1)", "Таможенное дело (гр 2)"]
            
            # Определяем индексы колонок
            time_col_idx = -1
            group_map = {} # {index: "Group Name"}
            
            for i, h in enumerate(headers):
                if h is None: continue
                h_clean = h.lower().replace('\n', ' ')
                if "время" in h_clean:
                    time_col_idx = i
                elif "день" in h_clean:
                    continue
                else:
                    # Это колонка группы
                    group_name = h.replace('\n', ' ').strip()
                    if len(group_name) > 2: # Игнорируем мусор
                        group_map[i] = group_name
                        if group_name not in schedule_by_group:
                            schedule_by_group[group_name] = {} # Dict[Day, List[Lesson]]

            if time_col_idx == -1:
                # Если шапка кривая, пробуем эвристику: 2-я колонка обычно время
                time_col_idx = 1
            
            # 2. Идем по строкам таблицы
            current_day = "Понедельник" # Дефолт
            
            for row in table[1:]:
                # Обработка ДНЯ НЕДЕЛИ (обычно 1-я колонка)
                # pdfplumber возвращает None в объединенных ячейках, кроме первой
                day_cell = row[0]
                if day_cell:
                    raw_day = day_cell.replace('\n', '').strip()
                    if len(raw_day) > 3: # Исключаем мусор
                        current_day = raw_day.capitalize()
                
                # Обработка ВРЕМЕНИ
                time_cell = row[time_col_idx]
                t_start, t_end = "", ""
                if time_cell:
                    tm = TIME_PATTERN.search(time_cell)
                    if tm:
                        t_start = tm.group(1).replace('.', ':')
                        t_end = tm.group(2).replace('.', ':')
                
                # Если времени нет в строке, возможно это "хвост" предыдущей пары
                # Но для упрощения пока пропускаем строки без времени
                if not t_start: 
                    continue

                # 3. Обработка ЯЧЕЕК ГРУПП
                for col_idx, group_name in group_map.items():
                    if col_idx >= len(row): continue
                    
                    cell_text = row[col_idx]
                    if not cell_text or len(cell_text.strip()) < 3:
                        continue
                    
                    # ПАРСИМ ЯЧЕЙКУ
                    parsed_lesson = _parse_cell_text(cell_text)
                    parsed_lesson.time_start = t_start
                    parsed_lesson.time_end = t_end
                    
                    # Добавляем в структуру
                    if current_day not in schedule_by_group[group_name]:
                        schedule_by_group[group_name][current_day] = []
                    
                    schedule_by_group[group_name][current_day].append(parsed_lesson)

    # Конвертируем в итоговый Pydantic формат
    final_output = {}
    for gr_name, days_dict in schedule_by_group.items():
        week_list = []
        for day, lessons in days_dict.items():
            week_list.append(DaySchedule(day_name=day, lessons=lessons))
        final_output[gr_name] = week_list

    return ParsedScheduleResponse(groups=final_output)

def _parse_cell_text(text: str) -> LessonItem:
    """Выкусывает данные из каши с помощью Regex"""
    text = text.replace('\n', ' ').strip()
    
    # 1. Тип занятия
    l_type = "Прак" # Дефолт
    tm = TYPE_PATTERN.search(text)
    if tm:
        l_type = tm.group(1).capitalize()
        text = text.replace(tm.group(0), "") # Удаляем из строки

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

    # 4. Предмет (всё, что осталось)
    # Чистим от лишних пробелов и точек
    subject = re.sub(r'\s+', ' ', text).strip(" .,-")
    
    if len(subject) < 2:
        subject = "Занятие" # Заглушка

    return LessonItem(
        subject=subject,
        type=l_type,
        teacher=teacher,
        room=room,
        time_start="", # Заполнится снаружи
        time_end=""
    )