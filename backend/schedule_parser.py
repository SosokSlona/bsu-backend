import pdfplumber
import re
import io
from typing import List, Dict, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
# Время: 08:30, 8.30
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})')

# Преподаватель (Улучшенная):
# 1. Фамилия (м.б. двойная)
# 2. Пробел
# 3. Инициалы (И. или И.О. или И. О.)
# Пример: Ходакова А.А., Соловей А.Н., Петров В. В.
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:-[A-ЯЁ][а-яё]+)?\s+[A-ЯЁ]\.\s?(?:[A-ЯЁ]\.)?)')

TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф|семинар)\)', re.IGNORECASE)
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/к|с/з|ауд\.?)\b', re.IGNORECASE)

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"🌊 [STREAM] Starting analysis. Size: {len(pdf_bytes)} bytes")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        start_page = max(0, (course - 1) * 2)
        pages = pdf.pages[start_page : start_page + 3] # Берем 3 страницы с запасом
        
        for page_num, page in enumerate(pages):
            print(f"📄 Processing Page {page_num + 1}...")
            width = page.width
            height = page.height
            
            # 1. Сбор слов
            words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=True)
            if not words: continue

            # 2. Поиск Времени (Ось Y)
            # Находим все Y-координаты, где есть время
            time_zones = []
            for w in words:
                if TIME_PATTERN.match(w['text']) and float(w['x0']) < 200: # Время слева
                    time_zones.append(w)
            
            if not time_zones:
                print("⚠️ No time slots found. Skipping page.")
                continue
                
            # Сортируем время и удаляем дубликаты (рядом стоящие)
            time_zones.sort(key=lambda w: w['top'])
            cleaned_times = []
            if time_zones:
                cleaned_times.append(time_zones[0])
                for t in time_zones[1:]:
                    if abs(t['top'] - cleaned_times[-1]['top']) > 15: # Новый слот
                        cleaned_times.append(t)
            
            # Верхняя граница таблицы (первое время)
            table_top = cleaned_times[0]['top'] - 10
            # Левая граница данных (справа от времени)
            data_left_boundary = max([t['x1'] for t in cleaned_times]) + 5

            # 3. Анализ Колонок (Метод "Потока")
            # Берем все слова, которые ВЫШЕ первого времени (Шапка) и ПРАВЕЕ времени
            header_words = [w for w in words if w['top'] < table_top and w['x0'] > data_left_boundary]
            
            # Ищем заголовки групп
            group_cols = []
            header_words.sort(key=lambda w: w['x0'])
            
            for i, w in enumerate(header_words):
                txt = w['text'].lower()
                # Логика: Ищем слово "Группа" или "Гр"
                if "груп" in txt or "гр." in txt:
                    # Пытаемся найти номер (в этом слове или соседнем)
                    g_num = ""
                    # "Группа13"
                    nums = re.findall(r'\d+', txt)
                    if nums: g_num = nums[0]
                    # "Группа" ... "13"
                    elif i+1 < len(header_words):
                        next_w = header_words[i+1]
                        if next_w['text'].isdigit(): g_num = next_w['text']
                    
                    if g_num:
                        # Центр колонки
                        center = (w['x0'] + w['x1']) / 2
                        group_cols.append({'name': g_num, 'center': center})

            # Если не нашли явные заголовки, ищем просто числа в шапке (Фолбэк)
            if not group_cols:
                for w in header_words:
                    if w['text'].isdigit() and len(w['text']) == 2: # 13, 14, 17...
                        # Исключаем года (20, 21, 22...)
                        val = int(w['text'])
                        if 1 <= val <= 30: # Разумный диапазон групп
                             group_cols.append({'name': w['text'], 'center': (w['x0'] + w['x1'])/2})

            # Удаляем дубликаты (если одна группа найдена дважды)
            unique_cols = []
            if group_cols:
                group_cols.sort(key=lambda g: g['center'])
                unique_cols.append(group_cols[0])
                for g in group_cols[1:]:
                    if abs(g['center'] - unique_cols[-1]['center']) > 50:
                        unique_cols.append(g)
            group_cols = unique_cols
            
            print(f"   🏛️ Groups Found: {[g['name'] for g in group_cols]}")
            if not group_cols: continue

            # Вычисляем границы колонок (середина между центрами)
            col_boundaries = [] # [(x_start, x_end, name)]
            for i in range(len(group_cols)):
                # Левая граница
                if i == 0:
                    left = data_left_boundary
                else:
                    left = (group_cols[i-1]['center'] + group_cols[i]['center']) / 2
                
                # Правая граница
                if i == len(group_cols) - 1:
                    right = width
                else:
                    right = (group_cols[i]['center'] + group_cols[i+1]['center']) / 2
                
                col_boundaries.append({'name': group_cols[i]['name'], 'x0': left, 'x1': right})

            # 4. Парсинг Строк
            current_day = "Понедельник"
            
            for i, t_slot in enumerate(cleaned_times):
                # Границы строки по Y
                row_top = t_slot['top'] - 5
                row_bottom = cleaned_times[i+1]['top'] - 5 if i < len(cleaned_times)-1 else height
                
                # Ищем день недели слева
                row_words_all = [w for w in words if row_top <= w['top'] < row_bottom]
                left_words = [w for w in row_words_all if w['x1'] < data_left_boundary]
                
                for lw in left_words:
                    d_txt = lw['text'].lower()
                    for dname in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                        if dname in d_txt: current_day = dname.capitalize()

                # Время
                time_str = t_slot['text'] # "8.30"
                # Пытаемся найти конец пары (например "-9.50")
                time_end_part = ""
                for w in left_words:
                    if w != t_slot and abs(w['top'] - t_slot['top']) < 15 and w['x0'] > t_slot['x0']:
                        time_end_part = w['text']
                
                full_time = time_str + time_end_part
                t_matches = TIME_PATTERN.findall(full_time)
                t_start = t_matches[0].replace('.', ':') if len(t_matches) > 0 else ""
                t_end = t_matches[1].replace('.', ':') if len(t_matches) > 1 else ""

                # Разбор ячеек
                for col in col_boundaries:
                    # Слова внутри ячейки
                    cell_words = []
                    for w in row_words_all:
                        w_center = (w['x0'] + w['x1']) / 2
                        # Попадание в колонку
                        if col['x0'] <= w_center < col['x1']:
                            cell_words.append(w)
                        # ЛЕКЦИЯ: Перекрытие границ
                        elif w['x0'] < col['x0'] and w['x1'] > col['x1']:
                            cell_words.append(w)
                    
                    if not cell_words: continue
                    
                    # Собираем текст
                    cell_words.sort(key=lambda w: (int(w['top']/5), w['x0']))
                    text = " ".join([w['text'] for w in cell_words])
                    
                    # Мусорный фильтр
                    if len(text) < 4 or "с/к" in text.lower(): continue
                    
                    # Парсим
                    lessons = _parse_cell_text(text)
                    
                    # Сохраняем
                    g_key = f"Группа {col['name']}"
                    if g_key not in schedule_by_group: schedule_by_group[g_key] = {}
                    if current_day not in schedule_by_group[g_key]: schedule_by_group[g_key][current_day] = []
                    
                    for l in lessons:
                        l.time_start = t_start
                        l.time_end = t_end
                        # Проверка дублей
                        exists = any(x.subject == l.subject and x.time_start == l.time_start for x in schedule_by_group[g_key][current_day])
                        if not exists:
                            schedule_by_group[g_key][current_day].append(l)

    # Финал
    final = {}
    d_ord = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    for g, d in schedule_by_group.items():
        week = []
        for dn in sorted(d.keys(), key=lambda x: d_ord.index(x) if x in d_ord else 9):
            week.append(DaySchedule(day_name=dn, lessons=d[dn]))
        final[g] = week
        
    return ParsedScheduleResponse(groups=final)

def _parse_cell_text(text: str) -> List[LessonItem]:
    text = text.replace('\n', ' ').strip()
    
    # 1. Тип
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        v = tm.group(1).lower()
        if "лек" in v: l_type = "Лекция"
        elif "сем" in v: l_type = "Семинар"
        elif "лаб" in v: l_type = "Лаба"
        text = text.replace(tm.group(0), "")

    # 2. Аудитория
    room = ""
    rm = ROOM_PATTERN.search(text)
    if rm:
        room = rm.group(0)
        text = text.replace(room, "")

    # 3. Преподаватель (Жадный поиск ФИО)
    teacher = ""
    # Ищем все совпадения
    ts = list(TEACHER_PATTERN.finditer(text))
    if ts:
        # Обычно препод в конце строки
        t_match = ts[-1]
        teacher = t_match.group(0).strip()
        text = text[:t_match.start()] + text[t_match.end():] # Вырезаем

    # 4. Предмет
    subj = text.replace("—", "").replace("-", "").strip(" .,")
    if len(subj) < 3:
        if "англ" in text.lower(): subj = "Иностранный язык"
        elif "физ" in text.lower(): subj = "Физкультура"
        else: subj = "Занятие"

    # Подгруппа
    subg = None
    if "англ" in text.lower(): subg = "Английский"
    elif "нем" in text.lower(): subg = "Немецкий"
    elif "фр" in text.lower(): subg = "Французский"
    
    return [LessonItem(subject=subj, type=l_type, teacher=teacher, room=room, time_start="", time_end="", subgroup=subg)]