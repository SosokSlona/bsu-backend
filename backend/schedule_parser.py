import pdfplumber
import re
import io
from typing import List, Dict
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})')
# Ищем Фамилию и Инициалы. Учитываем:
# - Двойные фамилии (Кузьмина-Мамедова)
# - Отсутствие пробелов (ИвановИ.И.)
# - Иностранные имена (Самет Азап) - 2 слова с большой буквы
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:-[A-ЯЁ][а-яё]+)?\s+(?:[A-ЯЁ]\.\s?[A-ЯЁ]\.|[A-ЯЁ][а-яё]+)|[A-ЯЁ][а-яё]+[A-ЯЁ]\.[A-ЯЁ]\.)')
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф|семинар)\)', re.IGNORECASE)
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/к|с/з|ауд\.?)\b', re.IGNORECASE)

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"📐 [TIME-FIRST] Starting. Size: {len(pdf_bytes)}")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        start_page = max(0, (course - 1) * 2)
        pages = pdf.pages[start_page : start_page + 2]
        
        for page_num, page in enumerate(pages):
            print(f"📄 Analyzing Page {page_num + 1}...")
            words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=True)
            
            # --- ШАГ 1: НАХОДИМ ВРЕМЯ (ОПОРНАЯ ОСЬ) ---
            time_words = []
            for w in words:
                if TIME_PATTERN.search(w['text']):
                    # Валидация: время обычно слева (x < 200)
                    if float(w['x0']) < 200: 
                        time_words.append(w)
            
            if not time_words:
                print("⚠️ No time slots found on page (Is it a text PDF?). Skipping.")
                continue
            
            # Определяем границы таблицы
            # Верх таблицы - это Y первого времени минус отступ
            min_time_y = min([w['top'] for w in time_words])
            # Граница времени справа
            max_time_x = max([w['x1'] for w in time_words])
            
            print(f"   ⏰ Header Boundary found at Y={min_time_y:.1f}. Time Column ends at X={max_time_x:.1f}")

            # --- ШАГ 2: СКАНИРУЕМ ШАПКУ (Зона выше min_time_y) ---
            # Ищем группы правее времени
            header_words = [w for w in words if w['top'] < min_time_y and w['x0'] > max_time_x]
            
            # Сортируем слева направо
            header_words.sort(key=lambda w: w['x0'])
            
            # ДЕБАГ: Что мы видим в шапке?
            header_text_debug = " ".join([w['text'] for w in header_words])
            print(f"   🧐 Header Text Scan: '{header_text_debug}'")

            group_anchors = []
            
            for i, w in enumerate(header_words):
                txt = w['text'].lower()
                # 1. Явное слово "Группа"
                if "груп" in txt:
                    # Ищем цифру рядом (в этом слове или следующем)
                    g_num = ""
                    # "Группа13"
                    num_in_word = re.findall(r'\d+', txt)
                    if num_in_word:
                        g_num = num_in_word[0]
                    # "Группа" "13" (следующее слово)
                    elif i + 1 < len(header_words):
                        next_w = header_words[i+1]
                        if next_w['text'].isdigit():
                            g_num = next_w['text']
                    
                    if g_num:
                        center_x = (w['x0'] + w['x1']) / 2
                        group_anchors.append({'name': g_num, 'center': center_x})
                
                # 2. Просто цифра (на всякий случай, если "Группа" не прочиталась)
                # Но только если она "похожа" на группу (2 цифры) и мы еще не нашли её через слово "Группа"
                elif w['text'].isdigit() and len(w['text']) == 2:
                     # Проверяем, не добавили ли мы её уже
                     already_found = any(g['name'] == w['text'] for g in group_anchors)
                     if not already_found:
                         # Хак: считаем группой, только если это не год (20, 21...)
                         # Для надежности лучше полагаться на слово "Группа", но пока так
                         pass 

            if not group_anchors:
                print("⚠️ Groups not found in header. Trying brute-force search for 2-digit numbers...")
                # Фолбэк: ищем любые 2-значные числа в шапке
                for w in header_words:
                    if w['text'].isdigit() and len(w['text']) == 2:
                         # Исключаем года типа 20, 21 если они в дате, но тут сложно
                         group_anchors.append({'name': w['text'], 'center': (w['x0'] + w['x1'])/2})

            if not group_anchors:
                print("❌ Fatal: No groups detected. Skipping page.")
                continue

            # Уникальные группы (убираем дубли, если "Группа" и "13" оба сработали)
            # Сортируем по X
            group_anchors.sort(key=lambda g: g['center'])
            unique_groups = []
            if group_anchors:
                unique_groups.append(group_anchors[0])
                for g in group_anchors[1:]:
                    # Если центр далеко от предыдущего (> 50px), это новая группа
                    if g['center'] - unique_groups[-1]['center'] > 50:
                        unique_groups.append(g)
            
            group_anchors = unique_groups
            print(f"   🏛️ Groups Located: {[g['name'] for g in group_anchors]}")

            # Формируем колонки
            columns = []
            for i, g in enumerate(group_anchors):
                # Левая граница
                left = (group_anchors[i-1]['center'] + g['center']) / 2 if i > 0 else max_time_x
                # Правая граница
                right = (g['center'] + group_anchors[i+1]['center']) / 2 if i < len(group_anchors) - 1 else page.width
                
                columns.append({'name': g['name'], 'x0': left, 'x1': right})

            # --- ШАГ 3: СТРОИМ СЕТКУ ВРЕМЕНИ ---
            rows = []
            # Группируем слова времени в строки (по Y)
            time_words.sort(key=lambda w: w['top'])
            current_row_words = [time_words[0]]
            
            for w in time_words[1:]:
                if abs(w['top'] - current_row_words[-1]['top']) < 10:
                    current_row_words.append(w)
                else:
                    # Обрабатываем накопленную строку времени
                    rows.append(_process_time_row(current_row_words, page.width))
                    current_row_words = [w]
            rows.append(_process_time_row(current_row_words, page.width))
            
            # Уточняем границы строк (bottom = top следующей)
            for i in range(len(rows) - 1):
                rows[i]['bottom'] = rows[i+1]['top'] - 5
            rows[-1]['bottom'] = page.height # Последняя строка до конца

            # --- ШАГ 4: КВАНТОВАНИЕ (РАЗБОР ЯЧЕЕК) ---
            current_day = "Понедельник"
            
            for row in rows:
                # А. Ищем день недели (слева от времени или в районе времени)
                # Берем все слова в этой полосе Y, левее первой группы
                row_words_all = [w for w in words if w['top'] >= row['top'] and w['bottom'] <= row['bottom']]
                
                for w in row_words_all:
                    if w['x1'] < columns[0]['x0']: # Слева от данных
                        d_txt = w['text'].lower()
                        for dname in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                            if dname in d_txt:
                                current_day = dname.capitalize()

                # Б. Разбираем ячейки групп
                for col in columns:
                    cell_words = []
                    for w in row_words_all:
                        w_center = (w['x0'] + w['x1']) / 2
                        w_width = w['x1'] - w['x0']
                        col_width = col['x1'] - col['x0']
                        
                        # 1. Строго внутри
                        if col['x0'] <= w_center < col['x1']:
                            cell_words.append(w)
                        # 2. Лекция (Нависает над колонкой)
                        # Если слово перекрывает > 50% колонки
                        elif min(w['x1'], col['x1']) - max(w['x0'], col['x0']) > col_width * 0.5:
                            cell_words.append(w)

                    if not cell_words: continue
                    
                    # Сортируем и собираем
                    cell_words.sort(key=lambda w: (int(w['top']), w['x0']))
                    text = " ".join([w['text'] for w in cell_words])
                    
                    if len(text) < 3 or "с/к" in text.lower(): continue
                    
                    lessons = _parse_lesson_text(text)
                    
                    g_key = f"Группа {col['name']}"
                    if g_key not in schedule_by_group: schedule_by_group[g_key] = {}
                    if current_day not in schedule_by_group[g_key]: schedule_by_group[g_key][current_day] = []
                    
                    for l in lessons:
                        l.time_start = row['start']
                        l.time_end = row['end']
                        # Дубликаты (для лекций)
                        is_dup = any(x.subject == l.subject and x.time_start == l.time_start for x in schedule_by_group[g_key][current_day])
                        if not is_dup:
                            schedule_by_group[g_key][current_day].append(l)

    final_output = {}
    for g, d in schedule_by_group.items():
        week = []
        for dn, ls in d.items(): week.append(DaySchedule(day_name=dn, lessons=ls))
        final_output[g] = week
    
    print(f"✅ [DONE] Groups: {list(final_output.keys())}")
    return ParsedScheduleResponse(groups=final_output)

def _process_time_row(words, page_width):
    # Собираем текст времени (например "8.30" и "-9.50")
    words.sort(key=lambda w: w['x0'])
    text = "".join([w['text'] for w in words])
    
    # Ищем нормальное время
    matches = TIME_PATTERN.findall(text)
    start, end = "", ""
    if len(matches) >= 1: start = matches[0].replace('.', ':')
    if len(matches) >= 2: end = matches[1].replace('.', ':')
    
    top = min([w['top'] for w in words]) - 5
    # Bottom пока временный, будет обновлен
    return {'start': start, 'end': end, 'top': top, 'bottom': top + 50}

def _parse_lesson_text(text: str) -> List[LessonItem]:
    # Умный парсер: вырезаем известное
    orig = text
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
        
    # 2. Ауд
    room = ""
    rm = ROOM_PATTERN.search(text)
    if rm:
        room = rm.group(0)
        text = text.replace(room, "")
        
    # 3. Препод
    teacher = ""
    ts = list(TEACHER_PATTERN.finditer(text))
    if ts:
        t = ts[-1] # Последний (обычно в конце)
        teacher = t.group(0).strip()
        text = text.replace(teacher, "")
        
    # 4. Предмет
    subj = text.replace("-", "").strip(" .,")
    if len(subj) < 3:
        if "англ" in orig.lower(): subj = "Иностранный язык"
        else: subj = "Занятие"
        
    subg = None
    if "англ" in orig.lower(): subg = "Английский"
    elif "нем" in orig.lower(): subg = "Немецкий"
    
    return [LessonItem(subject=subj, type=l_type, teacher=teacher, room=room, time_start="", time_end="", subgroup=subg)]