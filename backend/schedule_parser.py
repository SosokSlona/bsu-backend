import pdfplumber
import re
import io
from typing import List, Dict, Any
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- КОНСТАНТЫ ---
# Допуск по Y (в пикселях), чтобы считать, что текст на одной строке
Y_TOLERANCE = 5 
# Минимальная ширина текста, чтобы считать его "Лекцией на поток"
LECTURE_WIDTH_THRESHOLD = 150 

# Регулярки
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
# Ищем "Группа" или просто цифры, если они в шапке
GROUP_HEADER_PATTERN = re.compile(r'(?:группа\s*)?(\d{2,3})', re.IGNORECASE)

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"📐 [GEOMETRY] Starting parsing... Size: {len(pdf_bytes)}")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        start_page = max(0, (course - 1) * 2)
        pages = pdf.pages[start_page : start_page + 2]
        
        for page_num, page in enumerate(pages):
            print(f"📄 Analyzing Page {page_num + 1} with Geometry...")
            
            # 1. Извлекаем ВСЕ слова с координатами
            # words = list of dicts: {'text':Str, 'x0':float, 'x1':float, 'top':float, 'bottom':float}
            words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=True)
            
            # 2. Ищем ЯКОРЯ ГРУПП (Коридоры X)
            # Ищем слова "Группа" или номера групп в верхней части страницы (top < 150)
            group_columns = [] # [{'name': '17', 'x0': 100, 'x1': 200}, ...]
            
            # Сортируем слова по Y, потом по X
            words.sort(key=lambda w: (w['top'], w['x0']))
            
            # Анализ шапки (первые 20% слов)
            header_words = [w for w in words if w['top'] < page.height * 0.2]
            
            # Пытаемся найти слово "Группа" и следующие за ним цифры
            for w in header_words:
                txt = w['text'].lower()
                if "группа" in txt or (w['x0'] > 100 and w['text'].isdigit() and len(w['text'])==2):
                    # Это потенциальная группа
                    # Очищаем имя
                    g_name = w['text'].replace("Группа", "").strip()
                    if not g_name: continue # Пустое слово "Группа", ищем цифру рядом (сложно, упростим)
                    
                    # Если нашли цифру "17"
                    if g_name.isdigit():
                        # Определяем границы коридора. 
                        # Предполагаем, что колонка идет от текущего x0 до следующей группы
                        group_columns.append({
                            'name': g_name,
                            'x0': float(w['x0']) - 10, # Чуть расширяем влево
                            'x1': float(w['x1']) + 50  # Временная правая граница
                        })

            # Уточняем правые границы коридоров
            group_columns.sort(key=lambda g: g['x0'])
            for i in range(len(group_columns) - 1):
                # Правая граница текущей = левая граница следующей
                group_columns[i]['x1'] = group_columns[i+1]['x0']
            
            # Последняя группа идет до конца страницы
            if group_columns:
                group_columns[-1]['x1'] = float(page.width)

            print(f"   🏛️ Found Vertical Corridors: {[g['name'] for g in group_columns]}")
            if not group_columns:
                print("   ⚠️ No groups found via geometry. Skipping page.")
                continue

            # 3. Ищем УРОВНИ ВРЕМЕНИ (Ось Y)
            time_rows = [] # [{'time': '8.30-9.50', 'top': 100, 'bottom': 120}]
            
            for w in words:
                # Ищем паттерн времени в тексте
                if TIME_PATTERN.search(w['text']):
                    # Проверяем, не дубликат ли это (рядом по Y)
                    is_duplicate = False
                    for existing in time_rows:
                        if abs(existing['top'] - w['top']) < 10:
                            is_duplicate = True
                            break
                    
                    if not is_duplicate:
                        # Чистим время
                        tm = TIME_PATTERN.search(w['text'])
                        t_str = f"{tm.group(1).replace('.', ':')} - {tm.group(2).replace('.', ':')}"
                        time_rows.append({
                            'time': t_str,
                            'top': float(w['top']),
                            'bottom': float(w['bottom'])
                        })
            
            # Сортируем по времени (сверху вниз)
            time_rows.sort(key=lambda t: t['top'])
            print(f"   ⏰ Found {len(time_rows)} time slots")

            # 4. МАТРИЦА ПЕРЕСЕЧЕНИЙ (Mapping)
            # Для каждого временного слота...
            current_day = "Понедельник" # Дефолт
            
            for i, t_row in enumerate(time_rows):
                # Определяем высоту строки: от текущего времени до следующего (или до конца)
                row_top = t_row['top'] - 5
                row_bottom = time_rows[i+1]['top'] - 5 if i < len(time_rows)-1 else page.height
                
                # Ищем день недели слева от времени (x < 100) внутри этой высоты
                day_candidates = [w for w in words 
                                  if w['x1'] < group_columns[0]['x0'] 
                                  and w['top'] >= row_top - 20 # Чуть выше смотрим
                                  and w['bottom'] <= row_bottom]
                
                for dc in day_candidates:
                    d_txt = dc['text'].lower()
                    for day_name in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                        if day_name in d_txt:
                            current_day = day_name.capitalize()
                            break

                # 5. СБОР УРОЖАЯ (Текст внутри ячеек)
                # Ищем слова, которые попадают в этот Y-диапазон
                row_words = [w for w in words if w['top'] >= row_top and w['top'] < row_bottom]
                
                # Распределяем слова по группам
                for group in group_columns:
                    # Слова, которые попадают в X-коридор этой группы
                    g_words = []
                    
                    for w in row_words:
                        # Центр слова
                        w_center = (w['x0'] + w['x1']) / 2
                        w_width = w['x1'] - w['x0']
                        
                        # Логика 1: Слово строго внутри колонки
                        is_inside = (w_center >= group['x0'] and w_center < group['x1'])
                        
                        # Логика 2 (ЛЕКЦИЯ): Слово очень широкое и накрывает колонку
                        # Если ширина слова > 80% ширины колонки и оно пересекает её
                        is_wide_lecture = False
                        if w_width > (group['x1'] - group['x0']) * 0.8:
                            # Проверяем пересечение отрезков [wx0, wx1] и [gx0, gx1]
                            overlap = max(0, min(w['x1'], group['x1']) - max(w['x0'], group['x0']))
                            if overlap > 20: # Если пересечение существенное
                                is_wide_lecture = True
                        
                        if is_inside or is_wide_lecture:
                            g_words.append(w)
                    
                    if not g_words: continue
                    
                    # Собираем текст из слов (сортируем по Y, потом по X)
                    g_words.sort(key=lambda w: (w['top'] // 5, w['x0'])) # Группируем по строкам (допуск 5px)
                    
                    full_text = _assemble_text(g_words)
                    
                    # Если мусор - пропускаем
                    if len(full_text) < 4 or "с/к" in full_text.lower(): continue
                    
                    # Парсим детали
                    lessons = _smart_parse_text(full_text)
                    
                    # Сохраняем
                    g_key = f"Группа {group['name']}"
                    if g_key not in schedule_by_group: schedule_by_group[g_key] = {}
                    if current_day not in schedule_by_group[g_key]: schedule_by_group[g_key][current_day] = []
                    
                    for l in lessons:
                        l.time_start = t_row['time'].split(' - ')[0]
                        l.time_end = t_row['time'].split(' - ')[1]
                        schedule_by_group[g_key][current_day].append(l)

    # Финальная сборка
    final_output = {}
    days_order = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    
    for g_name, days in schedule_by_group.items():
        week = []
        sorted_days = sorted(days.items(), key=lambda x: days_order.index(x[0]) if x[0] in days_order else 9)
        for d_name, lessons in sorted_days:
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    print(f"✅ [GEOMETRY] Done. Groups: {list(final_output.keys())}")
    return ParsedScheduleResponse(groups=final_output)

def _assemble_text(words: List[Dict]) -> str:
    """Собирает слова в предложения, учитывая отступы"""
    if not words: return ""
    lines = []
    current_line = [words[0]['text']]
    last_top = words[0]['top']
    
    for w in words[1:]:
        if abs(w['top'] - last_top) > 8: # Новая строка визуально
            lines.append(" ".join(current_line))
            current_line = []
            last_top = w['top']
        current_line.append(w['text'])
    
    lines.append(" ".join(current_line))
    return " ".join(lines)

def _smart_parse_text(text: str) -> List[LessonItem]:
    """
    Умный парсер строки: [Тип] [Предмет] [Препод] [Ауд]
    """
    # 1. Тип занятия
    l_type = "Прак"
    if "(лек)" in text.lower() or "лек." in text.lower(): l_type = "Лекция"
    elif "(сем)" in text.lower(): l_type = "Семинар"
    elif "(лаб)" in text.lower(): l_type = "Лаба"
    
    # 2. Аудитория (цифры в конце или с/к)
    room = ""
    # Ищем 3-4 цифры, возможно с буквой, стоящие отдельно
    room_match = re.search(r'\b(\d{3,4}[а-я]?|с/к)\b', text, re.IGNORECASE)
    if room_match:
        room = room_match.group(1)
        text = text.replace(room, "") # Вырезаем
    
    # 3. Преподаватель (Фамилия И.О.)
    teacher = ""
    # Паттерн: Заглавная + строчные + пробел + Заглавная. + Заглавная.
    teach_match = re.search(r'([A-ЯЁ][а-яё]+\s+[A-ЯЁ]\.\s?[A-ЯЁ]\.)', text)
    if teach_match:
        teacher = teach_match.group(1)
        text = text.replace(teacher, "") # Вырезаем
        
    # 4. Предмет (всё что осталось)
    # Удаляем мусорные слова
    text = re.sub(r'\(.*?\)', '', text) # Удаляем все в скобках (типы)
    text = text.replace("—", "").replace("-", "").strip()
    subject = re.sub(r'\s+', ' ', text).strip()
    
    if len(subject) < 3: subject = "Занятие"
    
    # Подгруппа
    subgroup = None
    if "англ" in subject.lower(): subgroup = "Английский"
    elif "нем" in subject.lower(): subgroup = "Немецкий"
    
    return [LessonItem(
        subject=subject,
        type=l_type,
        teacher=teacher,
        room=room,
        time_start="",
        time_end="",
        subgroup=subgroup
    )]