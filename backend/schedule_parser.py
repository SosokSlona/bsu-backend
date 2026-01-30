import pytesseract
from pdf2image import convert_from_bytes
import cv2
import numpy as np
import re
import time
from typing import List, Dict, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# Конфиг Tesseract: режим блока текста
TESS_CONFIG = r'--oem 3 --psm 6 -l rus+eng'

TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф)\)', re.IGNORECASE)

# ЧЕРНЫЙ СПИСОК (Включая перевертыши)
# Если в колонке встречается любое из этих слов — это НЕ группа.
FORBIDDEN_GROUP_WORDS = [
    'понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота', 'воскресенье',
    'день', 'дни', 'время', 'часы', 'начало', 'конец',
    'гревтеч', 'киньледеноп', 'адерс', 'кинротв', 'ацинт'
]

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    print(f"⏱️ [OCR] START Parsing. Bytes: {len(pdf_bytes)}")
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    if course < 1: course = 1
    start_page = (course - 1) * 2
    
    try:
        # DPI 200 — баланс скорости
        images = convert_from_bytes(pdf_bytes, dpi=200, first_page=start_page+1, last_page=start_page+2)
    except Exception as e:
        print(f"❌ PDF Convert Error: {e}")
        return ParsedScheduleResponse(groups={})

    for pg_num, img in enumerate(images):
        print(f"📄 Processing Page {pg_num+1}...")
        open_cv_image = np.array(img) 
        original_img = cv2.cvtColor(open_cv_image, cv2.COLOR_RGB2BGR)
        
        # 1. Жесткая обрезка шапки (верхние 12%)
        h, w, _ = original_img.shape
        crop_y = int(h * 0.12)
        roi_img = original_img[crop_y:h, 0:w]
        
        # 2. Поиск ячеек
        cells = _find_table_cells(roi_img)
        if not cells: continue
        rows = _group_cells_into_rows(cells)
        if not rows: continue

        # 3. Анализ структуры
        col_roles = _analyze_column_roles(rows[0], roi_img)
        day_col = col_roles.get('day')
        time_col = col_roles.get('time')
        # Изначально считаем группами всё, что не день и не время
        candidate_groups = col_roles.get('groups', [])
        
        # Fallback: Если время не нашли, берем 2-ю колонку
        if time_col is None and len(rows[0]) > 2:
            time_col = 1
        
        # Fallback: Если групп нет, берем всё правее времени
        if not candidate_groups: 
             t_idx = time_col if time_col is not None else 1
             for i in range(t_idx + 1, len(rows[0])):
                 candidate_groups.append(i)

        # 4. ФИЛЬТРАЦИЯ ФАНТОМОВ (Самое важное!)
        valid_groups = []
        for g_idx in candidate_groups:
            if not _is_forbidden_column(rows, g_idx, roi_img):
                valid_groups.append(g_idx)
            else:
                print(f"🗑️ Dropped phantom column {g_idx} (Trash/Day detected)")
        
        group_cols = valid_groups
        current_day = "Понедельник"

        # 5. Парсинг строк
        for row in rows[1:]:
            # День
            if day_col is not None:
                d_cell = _get_cell_at_col(row, day_col)
                if d_cell:
                    d_txt = _ocr_cell(roi_img, d_cell)
                    if _is_day_of_week(d_txt):
                        current_day = d_txt.capitalize()

            # Время
            t_cell = None
            if time_col is not None:
                t_cell = _get_cell_at_col(row, time_col)
            elif len(row) > 1:
                t_cell = row[1] # Попытка угадать

            if not t_cell: continue
            
            t_txt = _ocr_cell(roi_img, t_cell)
            t_match = TIME_PATTERN.search(t_txt)
            if not t_match: continue 
            
            t_start = t_match.group(1).replace('.', ':')
            t_end = t_match.group(2).replace('.', ':')

            # Группы
            for g_idx in group_cols:
                g_cell = _get_cell_at_col(row, g_idx)
                
                # Look Left (Лекции)
                final_cell = g_cell
                if _is_cell_empty(roi_img, g_cell):
                    start_scan = (time_col + 1) if time_col is not None else 1
                    for scan_idx in range(start_scan, g_idx):
                        neighbor = _get_cell_at_col(row, scan_idx)
                        if not _is_cell_empty(roi_img, neighbor):
                            n_txt = _ocr_cell(roi_img, neighbor).lower()
                            if "лек" in n_txt or "общ" in n_txt:
                                final_cell = neighbor
                                break
                
                if not final_cell: continue
                
                raw_text = _ocr_cell(roi_img, final_cell)
                if len(raw_text) < 3: continue

                g_name = _get_group_name_from_header(rows[0], g_idx, roi_img)
                lessons = _parse_lesson_text(raw_text)
                
                if g_name not in schedule_by_group: schedule_by_group[g_name] = {}
                if current_day not in schedule_by_group[g_name]: schedule_by_group[g_name][current_day] = []
                
                for l in lessons:
                    l.time_start = t_start
                    l.time_end = t_end
                    schedule_by_group[g_name][current_day].append(l)

    final_output = {}
    for g_name, days in schedule_by_group.items():
        if not days: continue
        week = []
        for d_name, lessons in days.items():
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week
    
    return ParsedScheduleResponse(groups=final_output)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def _find_table_cells(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                   cv2.THRESH_BINARY, 11, 2)
    thresh = 255 - thresh
    # Ядра для поиска линий
    kernel_len = np.array(img).shape[1] // 100
    ver_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_len))
    hor_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_len, 1))
    image_1 = cv2.erode(thresh, ver_kernel, iterations=3)
    vertical_lines = cv2.dilate(image_1, ver_kernel, iterations=3)
    image_2 = cv2.erode(thresh, hor_kernel, iterations=3)
    horizontal_lines = cv2.dilate(image_2, hor_kernel, iterations=3)
    combined = cv2.addWeighted(vertical_lines, 0.5, horizontal_lines, 0.5, 0.0)
    contours, _ = cv2.findContours(combined, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    cells = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w > 20 and h > 10 and w < img.shape[1] * 0.9: 
            cells.append((x, y, w, h))
    cells.sort(key=lambda b: (b[1] // 10, b[0]))
    return cells

def _group_cells_into_rows(cells):
    rows = []
    current_row = []
    if not cells: return []
    last_y = cells[0][1]
    sorted_cells = sorted(cells, key=lambda b: b[1])
    for box in sorted_cells:
        x, y, w, h = box
        if abs(y - last_y) > 20:
            current_row.sort(key=lambda b: b[0])
            rows.append(current_row)
            current_row = []
            last_y = y
        current_row.append(box)
    if current_row:
        current_row.sort(key=lambda b: b[0])
        rows.append(current_row)
    return rows

def _ocr_cell(img, rect):
    x, y, w, h = rect
    roi = img[y+2:y+h-2, x+2:x+w-2] # Padding
    if roi.size == 0: return ""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    text = pytesseract.image_to_string(binary, config=TESS_CONFIG)
    return text.replace('\n', ' ').strip()

def _analyze_column_roles(header_row, img):
    roles = {'groups': []}
    for i, rect in enumerate(header_row):
        txt = _ocr_cell(img, rect).lower()
        # Если в заголовке мусор - это Day/Time, но точно не группа
        if any(f in txt for f in FORBIDDEN_GROUP_WORDS):
            if any(d in txt for d in ['понедельник', 'вторник', 'дни']):
                roles['day'] = i
            elif any(t in txt for t in ['время', 'часы', '8.30']):
                roles['time'] = i
            continue
        
        if len(txt) > 2 and "специальность" not in txt:
            roles['groups'].append(i)
    return roles

def _is_forbidden_column(rows, col_idx, img):
    """Проверяет колонку на мусор. Возвращает True, если колонка плохая."""
    hits = 0
    empty_count = 0
    total_checked = 0
    
    # Проверяем первые 8 строк
    for r in rows[:8]:
        cell = _get_cell_at_col(r, col_idx)
        if cell:
            txt = _ocr_cell(img, cell).lower()
            if len(txt) < 3: empty_count += 1
            if any(f in txt for f in FORBIDDEN_GROUP_WORDS):
                hits += 1
            total_checked += 1
            
    # Если найден мусор или колонка почти пустая — это фантом
    if hits > 0: return True
    if total_checked > 0 and (empty_count / total_checked) > 0.8: return True 
    
    return False

def _get_cell_at_col(row, col_idx):
    if col_idx < len(row): return row[col_idx]
    return None

def _is_cell_empty(img, rect):
    x, y, w, h = rect
    roi = img[y:y+h, x:x+w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    non_zero = cv2.countNonZero(255 - gray)
    return non_zero < 40

def _get_group_name_from_header(header_row, col_idx, img):
    if col_idx >= len(header_row): return f"Группа {col_idx}"
    txt = _ocr_cell(img, header_row[col_idx])
    txt = txt.replace('\n', ' ').strip()
    if len(txt) < 2 or "специальность" in txt.lower():
        return f"Группа {col_idx}"
    return txt

def _is_day_of_week(text):
    t = text.lower()
    return any(d in t for d in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'])

def _parse_lesson_text(text: str) -> List[LessonItem]:
    text = re.sub(r'\s+', ' ', text).strip()
    teachers = list(TEACHER_PATTERN.finditer(text))
    if len(teachers) <= 1: return [_extract_single(text)]
    results = []
    base = text[:teachers[0].start()].strip()
    if len(base) < 3: base = "Иностр. язык"
    for i, match in enumerate(teachers):
        t_name = match.group(1)
        end = teachers[i+1].start() if i < len(teachers)-1 else len(text)
        prev_end = teachers[i-1].end() if i > 0 else 0
        chunk = text[prev_end:end]
        item = _extract_single(chunk)
        item.subject = base
        item.teacher = t_name
        # Подгруппа
        cl = chunk.lower()
        sub = f"Группа {i+1}"
        if "англ" in cl: sub = "Английский"
        elif "нем" in cl: sub = "Немецкий"
        elif "фран" in cl: sub = "Французский"
        elif "исп" in cl: sub = "Испанский"
        elif "кит" in cl: sub = "Китайский"
        item.subgroup = sub
        results.append(item)
    return results

def _extract_single(text):
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        val = tm.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "ф" in val: l_type = "Факультатив"
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
    subject = text.strip(" .,-")
    if len(subject) < 2: subject = "Занятие"
    return LessonItem(subject=subject, type=l_type, teacher=teacher, room=room, time_start="", time_end="")