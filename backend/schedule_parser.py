import pdfplumber
import re
import io
from typing import List, Dict
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---

# Время: 8.30-9.50
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')

# Преподаватель (ФИО):
# Фамилия (м.б. двойная) + Пробел + И. + (опц. пробел) + О.
# Пример: Иванов И.И., Петров-Водкин А. Б.
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:-[A-ЯЁ][а-яё]+)?\s+[A-ЯЁ]\.\s?[A-ЯЁ]\.)')

# Тип занятия
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф|семинар)\)', re.IGNORECASE)

# Аудитория
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/к|с/з|ауд\.?)\b', re.IGNORECASE)

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"📐 [STRICT] Starting parsing... Size: {len(pdf_bytes)}")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Берем 3 страницы, начиная с (курс-1)*2
        start_page = max(0, (course - 1) * 2)
        pages = pdf.pages[start_page : start_page + 3]
        
        for page_num, page in enumerate(pages):
            print(f"📄 Analyzing Page {page_num + 1}...")
            
            # Извлекаем слова для анализа шапки
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            
            # 1. ПОИСК КОЛОНОК ГРУПП (СТРОГИЙ)
            # Ищем слова "Группа" в верхней части страницы (top < 150)
            header_words = [w for w in words if w['top'] < 150]
            group_cols = [] # [{'name': '13', 'x0': 100, 'x1': 200}, ...]
            
            # Сортируем слова по X
            header_words.sort(key=lambda w: w['x0'])
            
            for i, w in enumerate(header_words):
                txt = w['text'].lower()
                # Если нашли слово "Группа"
                if "группа" in txt:
                    # Смотрим следующее слово - это должен быть номер
                    # Но иногда "Группа" и "13" это одно слово или разные
                    g_num = ""
                    
                    # Вариант "Группа13"
                    if len(txt) > 6 and txt.replace("группа", "").isdigit():
                        g_num = txt.replace("группа", "")
                        x0 = float(w['x0'])
                        x1 = float(w['x1'])
                        
                    # Вариант "Группа" ... "13" (следующее слово)
                    elif i + 1 < len(header_words):
                        next_w = header_words[i+1]
                        if next_w['text'].isdigit() and len(next_w['text']) in [1, 2, 3]:
                            g_num = next_w['text']
                            x0 = float(w['x0'])
                            # Расширяем зону до конца цифры
                            x1 = float(next_w['x1']) 
                            
                    if g_num:
                        # Нашли группу! Определяем её зону (коридор)
                        # Левая граница: начало слова "Группа" - 10px
                        # Правая граница: будет определена следующим заголовком
                        group_cols.append({
                            'name': g_num,
                            'x0': x0 - 10,
                            'x1': 0 # Пока неизвестно
                        })

            if not group_cols:
                print("⚠️ No 'Group' headers found. Skipping page.")
                continue

            # Устанавливаем правые границы коридоров
            for i in range(len(group_cols)):
                if i < len(group_cols) - 1:
                    # Правая граница = начало следующей группы
                    group_cols[i]['x1'] = group_cols[i+1]['x0']
                else:
                    # Последняя группа идет до конца страницы
                    group_cols[i]['x1'] = float(page.width)

            print(f"   🏛️ Groups Found: {[g['name'] for g in group_cols]}")

            # 2. ПОИСК ВРЕМЕНИ (Строки)
            time_rows = []
            words_sorted_y = sorted(words, key=lambda w: w['top'])
            
            for w in words_sorted_y:
                if TIME_PATTERN.search(w['text']):
                    # Проверка на дубликаты (одна и та же строка)
                    if not time_rows or abs(w['top'] - time_rows[-1]['top']) > 10:
                        tm = TIME_PATTERN.search(w['text'])
                        t_str = f"{tm.group(1).replace('.', ':')} - {tm.group(2).replace('.', ':')}"
                        time_rows.append({
                            'time': t_str,
                            'top': float(w['top']),
                            'bottom': float(w['bottom'])
                        })
            
            print(f"   ⏰ Time Slots: {len(time_rows)}")

            # 3. ПАРСИНГ ЯЧЕЕК
            current_day = "Понедельник"
            
            for i, t_row in enumerate(time_rows):
                # Высота строки: от текущего времени до следующего
                row_top = t_row['top'] - 5
                row_bottom = time_rows[i+1]['top'] - 5 if i < len(time_rows)-1 else float(page.height)
                
                # Поиск ДНЯ НЕДЕЛИ (слева от первой группы)
                first_group_x = group_cols[0]['x0']
                day_words = [w for w in words if w['top'] >= row_top - 20 and w['bottom'] <= row_bottom and w['x1'] < first_group_x]
                
                for dw in day_words:
                    d_txt = dw['text'].lower()
                    for d_name in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                        if d_name in d_txt:
                            current_day = d_name.capitalize()

                # СБОР ДАННЫХ ПО ГРУППАМ
                # Берем все слова в этой временной полосе
                row_words = [w for w in words if w['top'] >= row_top and w['top'] < row_bottom]
                
                for group in group_cols:
                    # Слова, попадающие в колонку группы
                    g_words = []
                    for w in row_words:
                        w_center = (w['x0'] + w['x1']) / 2
                        
                        # Строгое попадание в колонку
                        if group['x0'] <= w_center < group['x1']:
                            g_words.append(w)
                        
                        # ЛЕКЦИЯ (Широкий текст): Если слово начинается в этой колонке, но вылезает вправо
                        # Или начинается слева (в предыдущей), но залезает сюда
                        # Упрощение: если это лекция, она обычно дублируется текстом, 
                        # либо pdfplumber видит её как текст, пересекающий границы.
                        # Добавим слова, которые "накрывают" центр колонки
                        elif w['x0'] < group['x0'] and w['x1'] > group['x1']:
                             g_words.append(w)

                    if not g_words: continue
                    
                    # Собираем текст
                    # Сортируем: сначала Y (строки), потом X (слова в строке)
                    g_words.sort(key=lambda w: (int(w['top'] / 5), w['x0']))
                    
                    full_text = " ".join([w['text'] for w in g_words])
                    
                    # Фильтр мусора
                    if len(full_text) < 4 or "с/к" in full_text.lower(): continue
                    
                    # Парсим
                    lessons = _smart_parse_text(full_text)
                    
                    g_key = f"Группа {group['name']}"
                    if g_key not in schedule_by_group: schedule_by_group[g_key] = {}
                    if current_day not in schedule_by_group[g_key]: schedule_by_group[g_key][current_day] = []
                    
                    for l in lessons:
                        l.time_start = t_row['time'].split(' - ')[0]
                        l.time_end = t_row['time'].split(' - ')[1]
                        schedule_by_group[g_key][current_day].append(l)

    # Сборка
    final_output = {}
    day_order = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    
    for g_name, days in schedule_by_group.items():
        week = []
        sorted_days = sorted(days.items(), key=lambda x: day_order.index(x[0]) if x[0] in day_order else 10)
        for d_name, lessons in sorted_days:
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    print(f"✅ [STRICT] Done. Groups: {list(final_output.keys())}")
    return ParsedScheduleResponse(groups=final_output)

def _smart_parse_text(text: str) -> List[LessonItem]:
    """Умный парсер с приоритетом ФИО"""
    # 1. Тип занятия
    l_type = "Прак"
    type_match = TYPE_PATTERN.search(text)
    if type_match:
        val = type_match.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "экз" in val: l_type = "Экзамен"
        text = text.replace(type_match.group(0), " ")

    # 2. Аудитория
    room = ""
    room_match = ROOM_PATTERN.search(text)
    if room_match:
        room = room_match.group(1)
        text = text.replace(room, " ")

    # 3. ПРЕПОДАВАТЕЛЬ (ФИО) - Самое важное
    # Ищем все совпадения, берем последнее (обычно препод в конце)
    teachers = list(TEACHER_PATTERN.finditer(text))
    teacher = ""
    if teachers:
        # Берем последнего найденного, так как предмет обычно в начале
        t_match = teachers[-1]
        teacher = t_match.group(0).strip()
        # Удаляем из текста
        text = text[:t_match.start()] + text[t_match.end():]
    
    # 4. Предмет (Чистка)
    # Убираем лишние символы
    subject = text.replace("—", "").replace("-", "").strip()
    subject = re.sub(r'\s+', ' ', subject).strip()
    
    if len(subject) < 2: subject = "Занятие"
    
    # Подгруппа
    subgroup = None
    if "англ" in text.lower(): subgroup = "Английский"
    elif "нем" in text.lower(): subgroup = "Немецкий"
    elif "фр" in text.lower(): subgroup = "Французский"
    
    return [LessonItem(
        subject=subject,
        type=l_type,
        teacher=teacher,
        room=room.strip(),
        time_start="",
        time_end="",
        subgroup=subgroup
    )]