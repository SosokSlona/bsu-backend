import pdfplumber
import re
import io
from typing import List, Dict, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРНЫЕ ВЫРАЖЕНИЯ (МАТЕМАТИКА ТЕКСТА) ---

# Время: 8.30-9.50, 08:30 - 09:50
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')

# Аудитория: 3-4 цифры, или "с/к", "с/з", "ауд."
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/к|с/з|ауд\.?)\b', re.IGNORECASE)

# Тип занятия: (лек), (пр), (лаб), (сем)
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф|семинар)\)', re.IGNORECASE)

# Преподаватель: Фамилия И.О. (с учетом двойных фамилий и отсутствия инициалов)
# Ищем паттерн: Заглавная буква, строчные, пробел, Заглавная, точка, Заглавная, точка
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:-[A-ЯЁ][а-яё]+)?\s+[A-ЯЁ]\.\s?[A-ЯЁ]\.)')

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"🚀 [PLUMBER] Starting analysis. Size: {len(pdf_bytes)} bytes")
    
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    # Открываем PDF как объект
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Выбираем страницы курса (обычно 2 страницы на курс)
        start_page_idx = max(0, (course - 1) * 2)
        # Берем с запасом 3 страницы, на случай смещения
        pages = pdf.pages[start_page_idx : start_page_idx + 2]
        
        for page_num, page in enumerate(pages):
            print(f"📄 Analyzing Page {page_num + 1}...")
            
            # Извлекаем таблицу с настройками для "грязных" PDF
            # vertical_strategy="text" помогает найти колонки по выравниванию текста
            tables = page.extract_tables({
                "vertical_strategy": "text", 
                "horizontal_strategy": "lines",
                "intersection_tolerance": 5
            })
            
            for table in tables:
                if not table or len(table) < 2: continue
                
                # --- ЭТАП 1: ПОИСК СТРУКТУРЫ (ЗАГОЛОВКИ) ---
                header_row_idx = -1
                group_map = {} # {column_index: "GroupName"}
                day_col_idx = -1
                time_col_idx = -1
                
                # Сканируем первые 5 строк, ищем "Часы" или "Время"
                for r_idx, row in enumerate(table[:5]):
                    row_text = " ".join([str(c).lower() for c in row if c])
                    if "часы" in row_text or "время" in row_text:
                        header_row_idx = r_idx
                        break
                
                if header_row_idx == -1:
                    print("⚠️ Header not found in table, skipping...")
                    continue

                # Анализируем найденную шапку
                header = table[header_row_idx]
                for c_idx, cell in enumerate(header):
                    if not cell: continue
                    txt = clean_str(cell).lower()
                    
                    if "дни" in txt or "день" in txt:
                        day_col_idx = c_idx
                    elif "часы" in txt or "время" in txt:
                        time_col_idx = c_idx
                    elif "группа" in txt or ("специальность" not in txt and len(txt) > 1):
                        # Это колонка группы!
                        # Чистим имя: "Группа 17" -> "Группа 17"
                        g_name = clean_str(cell)
                        # Защита от мусора в шапке
                        if len(g_name) < 20: 
                            group_map[c_idx] = g_name

                # Fallback: Если время не нашли, но таблица широкая, считаем 2-ю колонку временем
                if time_col_idx == -1 and len(header) > 2:
                    time_col_idx = 1
                
                print(f"   📊 Structure Found: TimeCol={time_col_idx}, Groups={list(group_map.values())}")

                # --- ЭТАП 2: ИТЕРАЦИЯ ПО СТРОКАМ ---
                current_day = "Понедельник"
                
                for row in table[header_row_idx + 1:]:
                    # 1. Определяем День (учитываем Merged Cells)
                    if day_col_idx != -1:
                        d_val = row[day_col_idx]
                        if d_val and len(d_val.strip()) > 2:
                            raw_day = clean_str(d_val).capitalize()
                            if is_valid_day(raw_day):
                                current_day = raw_day
                    
                    # 2. Определяем Время
                    t_val = row[time_col_idx] if time_col_idx != -1 else None
                    if not t_val: continue # Строка без времени — мусор или разделитель
                    
                    t_clean = clean_str(t_val)
                    t_match = TIME_PATTERN.search(t_clean)
                    if not t_match: continue
                    
                    t_start = t_match.group(1).replace('.', ':')
                    t_end = t_match.group(2).replace('.', ':')

                    # 3. Парсим Группы (Flood Fill Logic)
                    for col_idx in range(len(row)):
                        # Пропускаем день и время
                        if col_idx == day_col_idx or col_idx == time_col_idx: continue
                        
                        # Если это известная колонка группы
                        if col_idx in group_map:
                            g_name = group_map[col_idx]
                            cell_text = row[col_idx]
                            
                            # ЛОГИКА ОБЪЕДИНЕНИЯ (ЛЕКЦИИ)
                            # Если ячейка пустая, проверяем соседей слева.
                            # Если слева есть "Лекция", которая явно широкая, берем её.
                            # В pdfplumber merged cells часто возвращают None для "перекрытых" ячеек.
                            final_text = cell_text
                            
                            if not final_text:
                                # Ищем непустую ячейку слева в этой же строке, начиная от времени
                                for scan_i in range(col_idx - 1, time_col_idx, -1):
                                    neighbor = row[scan_i]
                                    if neighbor and len(neighbor) > 5:
                                        # Проверяем, похоже ли это на лекцию (обычно лекции объединяют потоки)
                                        if "(лек)" in neighbor.lower() or "лек." in neighbor.lower():
                                            final_text = neighbor
                                        break
                            
                            if not final_text or len(final_text.strip()) < 3: continue

                            # Парсим содержимое ячейки
                            lessons = parse_cell_content(final_text)
                            
                            # Сохраняем
                            if g_name not in schedule_by_group: schedule_by_group[g_name] = {}
                            if current_day not in schedule_by_group[g_name]: schedule_by_group[g_name][current_day] = []
                            
                            for l in lessons:
                                l.time_start = t_start
                                l.time_end = t_end
                                schedule_by_group[g_name][current_day].append(l)

    # --- ЭТАП 3: СБОРКА И СОРТИРОВКА ---
    final_output = {}
    day_order = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    
    for g_name, days_dict in schedule_by_group.items():
        week_schedule = []
        # Сортируем дни недели
        sorted_days = sorted(days_dict.items(), key=lambda x: day_order.index(x[0]) if x[0] in day_order else 10)
        
        for d_name, lessons in sorted_days:
            week_schedule.append(DaySchedule(day_name=d_name, lessons=lessons))
        
        final_output[g_name] = week_schedule

    print(f"✅ Parsing complete. Groups found: {list(final_output.keys())}")
    return ParsedScheduleResponse(groups=final_output)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def clean_str(s: str) -> str:
    if not s: return ""
    return s.replace('\n', ' ').strip()

def is_valid_day(s: str) -> bool:
    return any(d in s.lower() for d in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'])

def parse_cell_content(text: str) -> List[LessonItem]:
    """
    Умный парсер содержимого ячейки.
    Разделяет предметы, если их несколько (например, по разным неделям или подгруппам).
    """
    text = clean_str(text)
    
    # 1. Если есть явное разделение " / " или "Числитель/Знаменатель" (сложно, пока берем просто текст)
    # Попробуем найти всех преподавателей, чтобы разбить строку
    
    teachers_matches = list(TEACHER_PATTERN.finditer(text))
    
    # Если преподов нет или один — считаем это одним предметом
    if len(teachers_matches) <= 1:
        return [extract_lesson_details(text)]
    
    # Если преподов много, пытаемся разбить строку
    results = []
    # Эвристика: разбиваем по началу совпадения следующего преподавателя, 
    # но нужно найти начало ПРЕДМЕТА перед ним. Это сложно.
    # Упрощение: разбиваем строку пополам, если 2 препода. 
    # Но надежнее: просто вернуть всё как один предмет, но с длинным описанием.
    # Попробуем разделить по подгруппам (например "1. Англ... 2. Англ...")
    
    if "1." in text and "2." in text:
        # Попытка разбить по нумерации подгрупп
        parts = re.split(r'\b\d\.', text)
        for part in parts:
            if len(part) > 3:
                results.append(extract_lesson_details(part))
        if results: return results

    return [extract_lesson_details(text)]

def extract_lesson_details(raw_text: str) -> LessonItem:
    """
    Метод ВЫЧИТАНИЯ: Находим известное (ауд, тип, препод), удаляем, остаток — это предмет.
    """
    text = raw_text.strip()
    
    # 1. Вырезаем Тип занятия
    l_type = "Прак" # Дефолт
    type_match = TYPE_PATTERN.search(text)
    if type_match:
        val = type_match.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "экз" in val: l_type = "Экзамен"
        elif "ф" in val: l_type = "Факультатив"
        # Удаляем из текста
        text = text.replace(type_match.group(0), " ")

    # 2. Вырезаем Аудиторию (обычно в конце или после типа)
    room = ""
    room_match = ROOM_PATTERN.search(text)
    if room_match:
        room = room_match.group(1)
        text = text.replace(room, " ")

    # 3. Вырезаем Преподавателя
    teacher = ""
    teach_match = TEACHER_PATTERN.search(text)
    if teach_match:
        teacher = teach_match.group(1)
        text = text.replace(teacher, " ")
    
    # 4. Все что осталось — это Предмет
    # Чистим от мусора (тире, точки, лишние пробелы)
    subject = re.sub(r'\s+', ' ', text).strip(" .,-–")
    
    # Хак: Если предмет слишком короткий, возможно это "Иностр. язык"
    if len(subject) < 2 and "англ" in raw_text.lower(): subject = "Иностранный язык"
    if not subject: subject = "Занятие"

    # 5. Определение подгруппы
    subgroup = None
    lower_raw = raw_text.lower()
    if "англ" in lower_raw: subgroup = "Английский"
    elif "нем" in lower_raw: subgroup = "Немецкий"
    elif "фр" in lower_raw: subgroup = "Французский"
    elif "кит" in lower_raw: subgroup = "Китайский"
    elif "исп" in lower_raw: subgroup = "Испанский"
    
    return LessonItem(
        subject=subject,
        type=l_type,
        teacher=teacher.strip(),
        room=room.strip(),
        time_start="", # Будет заполнено выше
        time_end="",
        subgroup=subgroup
    )