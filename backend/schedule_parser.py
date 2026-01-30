import pytesseract
from pdf2image import convert_from_bytes
import cv2
import numpy as np
import re
from typing import List, Dict, Optional
from models import ParsedScheduleResponse, DaySchedule, LessonItem

# Настройка Tesseract (языки: русский + английский)
TESS_CONFIG = r'--oem 3 --psm 6 -l rus+eng'

# Регулярки для парсинга текста внутри ячейки
TIME_PATTERN = re.compile(r'(\d{1,2}[:.]\d{2})\s*[-–]\s*(\d{1,2}[:.]\d{2})')
ROOM_PATTERN = re.compile(r'\b(\d{3,4}[а-я]?|с/з|с/к|ауд\.?)\b', re.IGNORECASE)
TYPE_PATTERN = re.compile(r'\((лек|прак|сем|лаб|кcр|зачет|экз.*?|ф)\)', re.IGNORECASE)
TEACHER_PATTERN = re.compile(r'([A-ЯЁ][а-яё]+(?:\s+[A-ЯЁ]\.){1,2})')

def parse_schedule_pdf(pdf_bytes: bytes, course: int) -> ParsedScheduleResponse:
    schedule_by_group: Dict[str, Dict[str, List[LessonItem]]] = {}
    
    # 1. Конвертируем PDF в картинки (300 DPI для качества)
    # Выбираем страницы курса
    if course < 1: course = 1
    start_page = (course - 1) * 2
    
    try:
        images = convert_from_bytes(pdf_bytes, dpi=300, first_page=start_page+1, last_page=start_page+2)
    except Exception as e:
        print(f"PDF Convert Error: {e}")
        return ParsedScheduleResponse(groups={})

    for img in images:
        # Превращаем в массив numpy для OpenCV
        open_cv_image = np.array(img) 
        # Конвертируем RGB -> BGR (так любит OpenCV)
        original_img = cv2.cvtColor(open_cv_image, cv2.COLOR_RGB2BGR)
        
        # 2. Обработка изображения (Поиск сетки таблицы)
        # Отрезаем шапку (верхние 12%), чтобы убрать название специальности
        height, width, _ = original_img.shape
        crop_y = int(height * 0.12)
        roi_img = original_img[crop_y:height, 0:width]
        
        # Находим ячейки
        cells = _find_table_cells(roi_img)
        if not cells: continue

        # 3. Определяем структуру колонок
        # Сортируем ячейки по Y (строки), потом по X (колонки)
        # Нам нужно найти колонку Времени и Дня
        
        # Группируем ячейки в строки
        rows = _group_cells_into_rows(cells)
        if not rows: continue

        # Анализируем первую строку (шапку таблицы)
        col_roles = _analyze_column_roles(rows[0], roi_img)
        day_col = col_roles.get('day')
        time_col = col_roles.get('time')
        group_cols = col_roles.get('groups', [])

        if time_col is None or not group_cols: continue

        current_day = "Понедельник"

        # 4. Проходим по строкам данных
        for row in rows[1:]:
            # А. День
            if day_col is not None:
                d_cell = _get_cell_at_col(row, day_col)
                if d_cell:
                    d_text = _ocr_cell(roi_img, d_cell)
                    if _is_day_of_week(d_text):
                        current_day = d_text.capitalize()

            # Б. Время
            t_cell = _get_cell_at_col(row, time_col)
            if not t_cell: continue
            t_text = _ocr_cell(roi_img, t_cell)
            t_match = TIME_PATTERN.search(t_text)
            if not t_match: continue # Строка без времени - мусор
            
            t_start = t_match.group(1).replace('.', ':')
            t_end = t_match.group(2).replace('.', ':')

            # В. Группы
            for g_idx in group_cols:
                g_cell = _get_cell_at_col(row, g_idx)
                
                # Логика Look Left (Лекции)
                # Если ячейка пустая, проверяем соседей слева
                final_cell = g_cell
                if _is_cell_empty(roi_img, g_cell):
                    for scan_idx in range(time_col + 1, g_idx):
                        neighbor = _get_cell_at_col(row, scan_idx)
                        if not _is_cell_empty(roi_img, neighbor):
                            n_txt = _ocr_cell(roi_img, neighbor).lower()
                            if "лек" in n_txt or "общ" in n_txt:
                                final_cell = neighbor
                                break
                
                if not final_cell: continue
                
                # РАСПОЗНАВАНИЕ ТЕКСТА (OCR)
                raw_text = _ocr_cell(roi_img, final_cell)
                if len(raw_text) < 3: continue

                # Название группы
                # Берем из шапки (первая строка таблицы)
                g_name = _get_group_name_from_header(rows[0], g_idx, roi_img)
                
                # Парсинг предмета
                lessons = _parse_lesson_text(raw_text)
                
                if g_name not in schedule_by_group:
                    schedule_by_group[g_name] = {}
                if current_day not in schedule_by_group[g_name]:
                    schedule_by_group[g_name][current_day] = []
                
                for l in lessons:
                    l.time_start = t_start
                    l.time_end = t_end
                    schedule_by_group[g_name][current_day].append(l)

    # Сборка ответа
    final_output = {}
    for g_name, days in schedule_by_group.items():
        week = []
        for d_name, lessons in days.items():
            week.append(DaySchedule(day_name=d_name, lessons=lessons))
        final_output[g_name] = week

    return ParsedScheduleResponse(groups=final_output)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ КОМПЬЮТЕРНОГО ЗРЕНИЯ ---

def _find_table_cells(img):
    """Находит координаты ячеек таблицы с помощью OpenCV"""
    # ЧБ + Бинаризация
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                   cv2.THRESH_BINARY, 11, 2)
    thresh = 255 - thresh # Инверсия

    # Ищем вертикальные и горизонтальные линии
    kernel_len = np.array(img).shape[1] // 100
    ver_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, kernel_len))
    hor_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_len, 1))

    # Вертикальные линии
    image_1 = cv2.erode(thresh, ver_kernel, iterations=3)
    vertical_lines = cv2.dilate(image_1, ver_kernel, iterations=3)

    # Горизонтальные линии
    image_2 = cv2.erode(thresh, hor_kernel, iterations=3)
    horizontal_lines = cv2.dilate(image_2, hor_kernel, iterations=3)

    # Сетка
    combined = cv2.addWeighted(vertical_lines, 0.5, horizontal_lines, 0.5, 0.0)
    
    # Находим контуры (ячейки)
    contours, _ = cv2.findContours(combined, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    
    cells = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        if w > 20 and h > 10 and w < img.shape[1] * 0.9: # Фильтр мусора
            cells.append((x, y, w, h))
            
    # Сортируем: сверху-вниз, слева-направо
    cells.sort(key=lambda b: (b[1] // 10, b[0]))
    return cells

def _group_cells_into_rows(cells):
    """Группирует ячейки в строки на основе Y-координаты"""
    rows = []
    current_row = []
    if not cells: return []
    
    last_y = cells[0][1]
    
    # Сортируем строго по Y
    sorted_cells = sorted(cells, key=lambda b: b[1])
    
    for box in sorted_cells:
        x, y, w, h = box
        if abs(y - last_y) > 20: # Новая строка
            # Сортируем текущую строку по X перед добавлением
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
    """Вырезает кусочек картинки и читает текст с помощью Tesseract"""
    x, y, w, h = rect
    # Добавляем небольшой отступ (padding)
    roi = img[y+2:y+h-2, x+2:x+w-2]
    if roi.size == 0: return ""
    
    # Предобработка для OCR (Увеличение контраста)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    
    text = pytesseract.image_to_string(binary, config=TESS_CONFIG)
    return text.replace('\n', ' ').strip()

def _analyze_column_roles(header_row, img):
    """Определяет, какая колонка - День, какая - Время"""
    roles = {'groups': []}
    
    for i, rect in enumerate(header_row):
        txt = _ocr_cell(img, rect).lower()
        
        if any(d in txt for d in ['понедельник', 'день', 'дни']):
            roles['day'] = i
        elif any(t in txt for t in ['время', 'часы', '8.30']):
            roles['time'] = i
        elif len(txt) > 2 and "специальность" not in txt:
            # Считаем группой, если это не мусор
            roles['groups'].append(i)
            
    return roles

def _get_cell_at_col(row, col_idx):
    """Возвращает ячейку, которая геометрически соответствует колонке"""
    # В OpenCV таблицы сложные, ячеек может не быть.
    # Простая эвристика: берем i-ый элемент, если таблица ровная.
    # Для улучшения можно сравнивать X-координаты, но пока так.
    if col_idx < len(row):
        return row[col_idx]
    return None

def _is_cell_empty(img, rect):
    x, y, w, h = rect
    roi = img[y:y+h, x:x+w]
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    # Считаем количество черных пикселей
    non_zero = cv2.countNonZero(255 - gray) # Инвертируем (текст черный)
    return non_zero < 50 # Если мало пикселей - пусто

def _get_group_name_from_header(header_row, col_idx, img):
    if col_idx >= len(header_row): return f"Группа {col_idx}"
    txt = _ocr_cell(img, header_row[col_idx])
    
    # Чистка
    txt = txt.replace('\n', ' ').strip()
    if len(txt) < 3 or "специальность" in txt.lower():
        return f"Группа {col_idx}"
    return txt

def _is_day_of_week(text):
    t = text.lower()
    return any(d in t for d in ['понедельник', 'вторник', 'среда', 'четверг', 'пятница', 'суббота'])

def _parse_lesson_text(text: str) -> List[LessonItem]:
    """Разбивает текст ячейки на уроки (уже без киньледеноП!)"""
    # Базовая чистка
    text = re.sub(r'\s+', ' ', text).strip()
    
    # Поиск преподов
    teachers = list(TEACHER_PATTERN.finditer(text))
    
    if len(teachers) <= 1:
        return [_extract_single(text)]
        
    results = []
    # Название (обычно Иностр. язык)
    base = text[:teachers[0].start()].strip()
    if len(base) < 3: base = "Иностр. язык"
    
    for i, match in enumerate(teachers):
        t_name = match.group(1)
        start = match.start()
        end = teachers[i+1].start() if i < len(teachers)-1 else len(text)
        prev_end = teachers[i-1].end() if i > 0 else 0
        
        chunk = text[prev_end:end]
        # Вытаскиваем инфу
        item = _extract_single(chunk)
        item.subject = base # Общее название
        item.teacher = t_name
        
        # Подгруппа
        chunk_lower = chunk.lower()
        sub = f"Группа {i+1}"
        if "англ" in chunk_lower: sub = "Английский"
        elif "нем" in chunk_lower: sub = "Немецкий"
        elif "фран" in chunk_lower: sub = "Французский"
        elif "исп" in chunk_lower: sub = "Испанский"
        
        item.subgroup = sub
        results.append(item)
        
    return results

def _extract_single(text):
    # Тип
    l_type = "Прак"
    tm = TYPE_PATTERN.search(text)
    if tm:
        val = tm.group(1).lower()
        if "лек" in val: l_type = "Лекция"
        elif "сем" in val: l_type = "Семинар"
        elif "лаб" in val: l_type = "Лаба"
        elif "ф" in val: l_type = "Факультатив"
        text = text.replace(tm.group(0), "")

    # Ауд
    room = ""
    rm = ROOM_PATTERN.search(text)
    if rm:
        room = rm.group(1)
        text = text.replace(room, "")

    # Препод
    teacher = ""
    tcm = TEACHER_PATTERN.search(text)
    if tcm:
        teacher = tcm.group(1)
        text = text.replace(teacher, "")

    subject = text.strip(" .,-")
    if len(subject) < 2: subject = "Занятие"
    
    return LessonItem(subject=subject, type=l_type, teacher=teacher, room=room, time_start="", time_end="")