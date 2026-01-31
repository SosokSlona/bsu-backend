import pdfplumber
import re
import io
from typing import List, Dict, Tuple
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ (Regex) ---
# Время: 8.30, 08:30, 8.30-9.50
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})')

# Преподаватель:
# 1. Классика: Иванов И.И.
# 2. Иностранец: Самет Азап (Два слова с большой буквы в конце строки)
# 3. Двойная: Кузьмина-Мамедова
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:-[A-ЯЁ][а-яё]+)?\s+(?:[A-ЯЁ]\.\s?[A-ЯЁ]\.|[A-ЯЁ][а-яё]+))')

# Тип занятия
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф|семинар)\)', re.IGNORECASE)

# Аудитория: 3-4 цифры, с/к, ауд
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/к|с/з|ауд\.?)\b', re.IGNORECASE)

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"📐 [SPATIAL] Starting analysis. Size: {len(pdf_bytes)} bytes")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        # Берем страницы курса. Обычно это 2 страницы.
        start_page = max(0, (course - 1) * 2)
        pages = pdf.pages[start_page : start_page + 2]
        
        for page_num, page in enumerate(pages):
            print(f"📄 Analyzing Page {page_num + 1}...")
            width = page.width
            height = page.height
            
            # 1. ИЗВЛЕКАЕМ ВСЕ СЛОВА С КООРДИНАТАМИ
            # x0, top, x1, bottom, text
            words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=True)
            
            # 2. ПОИСК ОСИ X (КОЛОНКИ ГРУПП)
            # Ищем слова "Группа" в верхней части (top < 200)
            header_words = [w for w in words if w['top'] < 200]
            group_anchors = [] # {'name': '13', 'x0': 100, 'x1': 200}
            
            # Склеиваем слова "Группа" и "13", если они разбиты
            for i, w in enumerate(header_words):
                txt = w['text'].lower()
                if "группа" in txt:
                    # Пытаемся найти номер рядом или внутри
                    g_num = ""
                    # Вариант "Группа 13" (разные слова)
                    if i + 1 < len(header_words):
                        next_w = header_words[i+1]
                        if next_w['text'].isdigit():
                            g_num = next_w['text']
                            # Центр колонки - это середина слова "13"
                            center_x = (next_w['x0'] + next_w['x1']) / 2
                            group_anchors.append({'name': g_num, 'center': center_x})
                    
                    # Вариант "Группа13" (слитно)
                    elif len(txt) > 6 and any(c.isdigit() for c in txt):
                         g_num = re.sub(r'\D', '', txt)
                         center_x = (w['x0'] + w['x1']) / 2
                         group_anchors.append({'name': g_num, 'center': center_x})

            if not group_anchors:
                print("⚠️ No groups found on page. Skipping.")
                continue
            
            # Сортируем группы слева направо
            group_anchors.sort(key=lambda g: g['center'])
            
            # Определяем границы колонок (середина между центрами)
            # column[i] идет от (center[i-1] + center[i])/2 до (center[i] + center[i+1])/2
            columns = []
            for i, g in enumerate(group_anchors):
                # Левая граница
                if i == 0:
                    left = g['center'] - 100 # Отступ влево для первой группы
                else:
                    left = (group_anchors[i-1]['center'] + g['center']) / 2
                
                # Правая граница
                if i == len(group_anchors) - 1:
                    right = width # До конца страницы
                else:
                    right = (g['center'] + group_anchors[i+1]['center']) / 2
                
                columns.append({
                    'name': g['name'],
                    'x0': left,
                    'x1': right
                })
            
            print(f"   🏛️ Columns mapped: {[c['name'] for c in columns]}")

            # 3. ПОИСК ОСИ Y (ВРЕМЯ)
            time_anchors = []
            # Ищем текст похожий на время
            for w in words:
                if TIME_PATTERN.match(w['text']):
                    # Группируем близкие времена (8.30 и 9.50 - это одна строка)
                    y_center = (w['top'] + w['bottom']) / 2
                    
                    # Проверяем, есть ли уже такая строка
                    exists = False
                    for t in time_anchors:
                        if abs(t['y'] - y_center) < 15: # Погрешность 15px
                            exists = True
                            # Обновляем текст времени (склеиваем начало и конец)
                            if w['x0'] > t['x_max']: 
                                t['text'] += "-" + w['text']
                                t['x_max'] = w['x1']
                            break
                    
                    if not exists:
                        time_anchors.append({
                            'y': y_center,
                            'top': w['top'],
                            'text': w['text'],
                            'x_max': w['x1']
                        })
            
            # Сортируем время сверху вниз
            time_anchors.sort(key=lambda t: t['y'])
            
            # Создаем строки
            rows = []
            for i, t in enumerate(time_anchors):
                # Верхняя граница строки = верх времени
                row_top = t['top'] - 5
                # Нижняя граница = верх следующего времени (или низ страницы)
                if i < len(time_anchors) - 1:
                    row_bottom = time_anchors[i+1]['top'] - 5
                else:
                    row_bottom = height
                
                # Нормализация текста времени
                clean_time = t['text'].replace('.', ':')
                parts = clean_time.split('-')
                start = parts[0]
                end = parts[1] if len(parts) > 1 else ""
                
                rows.append({
                    'start': start,
                    'end': end,
                    'top': row_top,
                    'bottom': row_bottom
                })

            print(f"   ⏰ Found {len(rows)} time slots")

            # 4. РАСПРЕДЕЛЕНИЕ КОНТЕНТА
            # Проходим по каждой ячейке (Row x Column)
            
            current_day = "Понедельник"
            
            for row in rows:
                # А. Поиск дня недели в этой строке (слева от групп)
                # Ищем слова в левой части (x < columns[0]['x0']) и внутри Y-границ строки
                left_limit = columns[0]['x0']
                day_words = [w for w in words 
                             if w['top'] >= row['top'] and w['bottom'] <= row['bottom'] 
                             and w['x1'] < left_limit]
                
                for dw in day_words:
                    dt = dw['text'].lower()
                    for dname in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                        if dname in dt:
                            current_day = dname.capitalize()
                
                # Б. Поиск предметов для групп
                row_words = [w for w in words 
                             if w['top'] >= row['top'] and w['bottom'] <= row['bottom']]
                
                for col in columns:
                    # Слова, попадающие в колонку
                    cell_words = []
                    
                    for w in row_words:
                        w_center = (w['x0'] + w['x1']) / 2
                        
                        # 1. Строгое попадание
                        if col['x0'] <= w_center < col['x1']:
                            cell_words.append(w)
                        
                        # 2. Лекция (пересечение границ)
                        # Если слово начинается левее центра колонки и заканчивается правее центра
                        # Или слово очень широкое
                        elif w['x0'] < col['x0'] and w['x1'] > col['x1']:
                             cell_words.append(w) # Это лекция на весь поток
                    
                    if not cell_words: continue
                    
                    # Сортируем слова: Сначала Y (строки внутри ячейки), потом X
                    cell_words.sort(key=lambda w: (int(w['top'] / 5), w['x0']))
                    
                    # Собираем текст
                    full_text = " ".join([w['text'] for w in cell_words])
                    
                    # Фильтр мусора
                    if len(full_text) < 3 or "с/к" in full_text.lower(): continue
                    
                    # ПАРСИНГ ТЕКСТА
                    lessons = _spatial_text_parser(full_text)
                    
                    # Сохранение
                    g_key = f"Группа {col['name']}"
                    if g_key not in schedule_by_group: schedule_by_group[g_key] = {}
                    if current_day not in schedule_by_group[g_key]: schedule_by_group[g_key][current_day] = []
                    
                    for l in lessons:
                        l.time_start = row['start']
                        l.time_end = row['end']
                        # Проверка на дубликаты (лекции могут добавиться дважды из-за overlap)
                        exists = False
                        for existing in schedule_by_group[g_key][current_day]:
                            if existing.subject == l.subject and existing.time_start == l.time_start:
                                exists = True
                                break
                        if not exists:
                            schedule_by_group[g_key][current_day].append(l)

    # 5. СОРТИРОВКА И ВЫВОД
    final_output = {}
    d_order = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    
    for g_name, days in schedule_by_group.items():
        week = []
        s_days = sorted(days.items(), key=lambda x: d_order.index(x[0]) if x[0] in d_order else 9)
        for d, lessons in s_days:
            week.append(DaySchedule(day_name=d, lessons=lessons))
        final_output[g_name] = week

    print(f"✅ [SPATIAL] Done. Groups: {list(final_output.keys())}")
    return ParsedScheduleResponse(groups=final_output)

def _spatial_text_parser(text: str) -> List[LessonItem]:
    """
    Умный парсер строки.
    Стратегия: Найти и вырезать известное, остальное - Предмет.
    """
    original_text = text
    text = text.replace('\n', ' ').strip()
    
    # 1. Тип занятия
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        val = tm.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        text = text.replace(tm.group(0), " ")

    # 2. Аудитория (в конце)
    room = ""
    # Ищем аудиторию в конце строки
    rm_matches = list(ROOM_PATTERN.finditer(text))
    if rm_matches:
        last_rm = rm_matches[-1]
        room = last_rm.group(0)
        text = text[:last_rm.start()] + text[last_rm.end():]

    # 3. Преподаватель
    # Ищем паттерн ФИО. Берем ПОСЛЕДНИЙ, так как предмет обычно в начале.
    teacher = ""
    t_matches = list(TEACHER_PATTERN.finditer(text))
    if t_matches:
        last_t = t_matches[-1]
        teacher = last_t.group(0).strip()
        # Удаляем преподавателя из текста
        text = text[:last_t.start()] + text[last_t.end():]
    
    # 4. Предмет (всё что осталось)
    # Чистим от мусора
    subject = text.replace("—", "").replace("-", "").strip()
    subject = re.sub(r'\s+', ' ', subject).strip()
    
    # Если предмет слишком короткий или пустой, а в оригинале было "Англ", восстанавливаем
    if len(subject) < 3:
        if "англ" in original_text.lower(): subject = "Иностранный язык"
        elif "физ" in original_text.lower(): subject = "Физкультура"
        else: subject = "Занятие"

    # Подгруппа
    subgroup = None
    low = original_text.lower()
    if "англ" in low: subgroup = "Английский"
    elif "нем" in low: subgroup = "Немецкий"
    elif "фр" in low: subgroup = "Французский"
    elif "кит" in low: subgroup = "Китайский"
    
    return [LessonItem(
        subject=subject,
        type=l_type,
        teacher=teacher,
        room=room,
        time_start="",
        time_end="",
        subgroup=subgroup
    )]