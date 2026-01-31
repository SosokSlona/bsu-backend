import fitz  # PyMuPDF
import re
from typing import List, Dict
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# --- РЕГУЛЯРКИ ---
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})')
# Ищем паттерн: "Фамилия И.О." или "Имя (иностранное)"
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:-[A-ЯЁ][а-яё]+)?\s+(?:[A-ЯЁ]\.\s?[A-ЯЁ]\.|[A-ЯЁ][а-яё]+))')
# Тип занятия
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф|семинар)\)', re.IGNORECASE)
# Аудитория (3-4 цифры, с/к)
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/к|с/з|ауд\.?)\b', re.IGNORECASE)

# Запрещенные слова для названий групп (защита от мусора)
BAD_GROUP_NAMES = ["дни", "часы", "курс", "специальность", "форма"]

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"🚀 [PyMuPDF] Starting. Size: {len(pdf_bytes)}")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        # Страницы курса
        start_page = max(0, (course - 1) * 2)
        # Берем 3 страницы с запасом
        pages = list(doc)[start_page : start_page + 3]
        
        for p_num, page in enumerate(pages):
            print(f"📄 Page {p_num + 1}...")
            
            # Получаем структуру страницы (словарями)
            # flag=0 (текст), sort=True (порядок чтения)
            text_instances = page.get_text("words", sort=True)
            
            # 1. ПОИСК ВРЕМЕНИ (Ось Y)
            time_rows = []
            for x0, y0, x1, y1, text, block_no, line_no, word_no in text_instances:
                if x0 < 200 and TIME_PATTERN.match(text): # Время обычно слева
                    # Группируем время по строкам (допуск 10px по Y)
                    found = False
                    for tr in time_rows:
                        if abs(tr['y'] - y0) < 15:
                            tr['text'] += text
                            found = True
                            break
                    if not found:
                        time_rows.append({'y': y0, 'text': text, 'bottom': y1})
            
            if not time_rows:
                print("⚠️ No time found. Skipping.")
                continue
                
            # Уточняем границы строк времени
            time_rows.sort(key=lambda x: x['y'])
            for i in range(len(time_rows) - 1):
                time_rows[i]['end_y'] = time_rows[i+1]['y']
            time_rows[-1]['end_y'] = page.rect.height

            # Граница шапки - это верх первого времени
            header_limit_y = time_rows[0]['y']

            # 2. ПОИСК ГРУПП (Ось X)
            # Ищем текст выше header_limit_y
            header_words = [w for w in text_instances if w[3] < header_limit_y] # w[3] is bottom_y
            
            group_cols = []
            
            # Проход 1: Ищем слово "Группа"
            for i, w in enumerate(header_words):
                txt = w[4].lower()
                if "груп" in txt:
                    # Ищем число рядом (в этом слове или следующем)
                    g_num = ""
                    # "Группа13"
                    digits = re.findall(r'\d{2,3}', txt)
                    if digits: 
                        g_num = digits[0]
                        center = (w[0] + w[2]) / 2
                    # "Группа" "13"
                    elif i + 1 < len(header_words):
                        next_w = header_words[i+1]
                        if next_w[4].isdigit():
                            g_num = next_w[4]
                            center = (next_w[0] + next_w[2]) / 2
                    
                    if g_num and g_num not in BAD_GROUP_NAMES:
                        group_cols.append({'name': g_num, 'center': center})

            # Проход 2: Если не нашли слово "Группа", ищем просто 2-значные числа в шапке справа
            if not group_cols:
                print("⚠️ Explicit 'Group' headers missing. Searching for stand-alone numbers...")
                for w in header_words:
                    if w[0] > 150 and w[4].isdigit() and len(w[4]) == 2: # x > 150 (справа от дней)
                         group_cols.append({'name': w[4], 'center': (w[0] + w[2])/2})

            # Фильтр дубликатов (если одно и то же число найдено дважды рядом)
            group_cols.sort(key=lambda g: g['center'])
            unique_cols = []
            if group_cols:
                unique_cols.append(group_cols[0])
                for g in group_cols[1:]:
                    if abs(g['center'] - unique_cols[-1]['center']) > 50:
                        unique_cols.append(g)
            group_cols = unique_cols

            print(f"   🏛️ Groups: {[g['name'] for g in group_cols]}")
            
            if not group_cols: continue

            # Строим границы колонок
            final_columns = []
            for i, g in enumerate(group_cols):
                # Левая граница
                left = (group_cols[i-1]['center'] + g['center']) / 2 if i > 0 else 200 # 200 - отступ от времени
                # Правая граница
                right = (g['center'] + group_cols[i+1]['center']) / 2 if i < len(group_cols) - 1 else page.rect.width
                final_columns.append({'name': g['name'], 'x0': left, 'x1': right})

            # 3. РАСПРЕДЕЛЕНИЕ БЛОКОВ
            # Получаем текст БЛОКАМИ (это сохраняет структуру "Предмет Препод")
            blocks = page.get_text("blocks", sort=True)
            
            current_day = "Понедельник"
            
            for b in blocks:
                # b = (x0, y0, x1, y1, text, block_no, block_type)
                bx0, by0, bx1, by1, btext, _, _ = b
                
                # Чистим текст
                btext = btext.replace('\n', ' ').strip()
                if len(btext) < 3 or "с/к" in btext.lower(): continue

                # А. Определение дня недели (слева)
                if bx1 < 150: 
                    low = btext.lower()
                    for d in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота']:
                        if d in low: current_day = d.capitalize()
                    continue

                # Б. Определение времени (в какую строку попадает блок)
                # Блок попадает в строку, если его центр Y внутри строки
                b_center_y = (by0 + by1) / 2
                target_row = None
                for tr in time_rows:
                    if tr['y'] <= b_center_y <= tr['end_y']:
                        target_row = tr
                        break
                
                if not target_row: continue

                # В. Определение группы (Колонки)
                # Проверяем пересечение по X
                for col in final_columns:
                    # Логика пересечения:
                    # 1. Блок целиком внутри колонки
                    # 2. Блок (лекция) накрывает колонку более чем на 50% её ширины
                    
                    # Пересечение отрезков [bx0, bx1] и [col.x0, col.x1]
                    overlap_start = max(bx0, col['x0'])
                    overlap_end = min(bx1, col['x1'])
                    overlap_len = max(0, overlap_end - overlap_start)
                    
                    col_width = col['x1'] - col['x0']
                    
                    # Если блок внутри колонки ИЛИ блок перекрывает колонку (лекция)
                    if overlap_len > 0:
                        # Считаем это попаданием, если пересечение значительное (например, > 30% ширины блока находится здесь)
                        # Или для лекций: если блок покрывает центр колонки
                        col_center = (col['x0'] + col['x1']) / 2
                        
                        if (bx0 < col_center < bx1) or (overlap_len / (bx1 - bx0) > 0.5):
                            # ЭТО НАША ПАРА
                            # Парсим время
                            times = re.findall(r'\d{1,2}[:.]\d{2}', target_row['text'])
                            t_start = times[0].replace('.', ':') if len(times) > 0 else ""
                            t_end = times[1].replace('.', ':') if len(times) > 1 else ""
                            
                            # Парсим текст
                            lessons = _smart_parse(btext)
                            
                            key = f"Группа {col['name']}"
                            if key not in schedule_by_group: schedule_by_group[key] = {}
                            if current_day not in schedule_by_group[key]: schedule_by_group[key][current_day] = []
                            
                            # Добавляем (проверка на дубли)
                            for l in lessons:
                                l.time_start = t_start
                                l.time_end = t_end
                                exists = any(x.subject == l.subject and x.time_start == l.time_start for x in schedule_by_group[key][current_day])
                                if not exists:
                                    schedule_by_group[key][current_day].append(l)

    # Финал
    final = {}
    d_order = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
    for g, days in schedule_by_group.items():
        week = []
        for dname in sorted(days.keys(), key=lambda x: d_order.index(x) if x in d_order else 9):
            week.append(DaySchedule(day_name=dname, lessons=days[dname]))
        final[g] = week
        
    return ParsedScheduleResponse(groups=final)

def _smart_parse(text: str) -> List[LessonItem]:
    """Умный парсер PyMuPDF текста"""
    # Удаляем лишние тире, которые возникают при переносе
    text = text.replace("- ", "").strip()
    
    # 1. Тип
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        v = tm.group(1).lower()
        if "лек" in v: l_type = "Лекция"
        elif "сем" in v: l_type = "Семинар"
        elif "лаб" in v: l_type = "Лаба"
        text = text.replace(tm.group(0), "")

    # 2. Ауд (обычно в конце)
    room = ""
    rm = ROOM_PATTERN.findall(text)
    if rm:
        room = rm[-1] # Последнее похожее на аудиторию
        # Удаляем его из текста
        text = re.sub(re.escape(room), "", text)

    # 3. Препод (ФИО)
    teacher = ""
    ts = list(TEACHER_PATTERN.finditer(text))
    if ts:
        # Берем последнего найденного (предмет обычно в начале)
        t_match = ts[-1]
        teacher = t_match.group(0).strip()
        # Вырезаем
        text = text[:t_match.start()] + text[t_match.end():]

    # 4. Предмет (Чистка)
    # Удаляем мусор: лишние точки, запятые, тире
    subj = re.sub(r'^[.,\s—-]+|[.,\s—-]+$', '', text).strip()
    
    # "Англ. 1" -> Предмет: Иностр, Подгруппа: Англ
    subg = None
    orig_lower = text.lower()
    
    if len(subj) < 4:
        if "англ" in orig_lower: subj = "Иностранный язык"; subg = "Английский"
        elif "нем" in orig_lower: subj = "Иностранный язык"; subg = "Немецкий"
        elif "физ" in orig_lower: subj = "Физкультура"
        else: subj = "Занятие"
    else:
        # Если предмет длинный, проверим подгруппу внутри
        if "англ" in orig_lower: subg = "Английский"
        elif "нем" in orig_lower: subg = "Немецкий"
        elif "фр" in orig_lower: subg = "Французский"

    return [LessonItem(
        subject=subj,
        type=l_type,
        teacher=teacher,
        room=room,
        time_start="",
        time_end="",
        subgroup=subg
    )]