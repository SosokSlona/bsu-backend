import logging
import re
import base64
import requests
import uvicorn
import ddddocr
import fitz  # PyMuPDF
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional
import time

# --- НАСТРОЙКИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BSU_Backend")

app = FastAPI()
ocr = ddddocr.DdddOcr(show_ad=False)

# Настройки туннеля (localhost:1080 -> SSH -> Беларусь)
PROXIES = {
    "http": "socks5://localhost:1080",
    "https": "socks5://localhost:1080"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

SPECIALTY_MAP = {
    "международные отношения": "IR_timetable.pdf",
    "мировая экономика": "WE_timetable.pdf",
    "международное право": "IL_timetable.pdf",
    "таможенное дело": "CA_timetable.pdf",
    "востоковедение": "V_timetable.pdf",
    "международная конфликтология": "IC_timetable.pdf",
    "международная логистика": "ILOG_timetable.pdf",
    "африканистика": "AF_timetable.pdf",
    "менеджмент": "ITG_timetable.pdf"
}

# --- МОДЕЛИ ---
class LoginRequest(BaseModel):
    username: str
    password: str

class ScheduleRequest(BaseModel):
    cookies: Dict[str, str]
    period_id: Optional[str] = None

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def clean_text(text: Any) -> str:
    if not text: return ""
    return re.sub(r'\s+', ' ', str(text).replace('\xa0', ' ').strip())

def safe_get_attr(element: Any, attr: str) -> str:
    if not element: return ""
    val = element.get(attr)
    return " ".join(val) if isinstance(val, list) else str(val) if val else ""

def parse_grade(cols: List[Any]) -> Dict[str, str]:
    """Умный парсинг оценок."""
    res = {"mark": "", "color_type": "neutral"}
    exam_cell = None
    credit_cell = None
    
    for c in cols:
        cls = safe_get_attr(c, "class")
        if "styleExamBody" in cls: exam_cell = c
        if "styleZachBody" in cls: credit_cell = c
    
    # 1. Экзамен
    if exam_cell:
        txt = clean_text(exam_cell.get_text()) or safe_get_attr(exam_cell, "title")
        if txt:
            # Ищем цифру в начале (напр. "9 (девять)")
            m = re.search(r'^(\d+)', txt)
            if m:
                res["mark"] = m.group(1)
                try:
                    v = int(res["mark"])
                    if v < 4: res["color_type"] = "bad"
                    elif v < 6: res["color_type"] = "bad"     # 4-5: Красный
                    elif v < 7: res["color_type"] = "neutral" # 6: Желтый
                    elif v < 9: res["color_type"] = "neutral" # 7-8: Желтоватый (логику цветов можно править тут)
                    else: res["color_type"] = "good"          # 9-10: Зеленый
                except: pass
            else:
                # Если цифры нет, берем текст (напр. "неявка")
                res["mark"] = txt[:15]
            return res

    # 2. Зачет
    if credit_cell:
        txt = clean_text(credit_cell.get_text())
        if txt:
            txt_lower = txt.lower()
            if any(x in txt_lower for x in ["зачтено", "+"]):
                res["mark"] = "Зачет"
                res["color_type"] = "good"
            elif any(x in txt_lower for x in ["не зачтено", "-"]):
                res["mark"] = "Незач"
                res["color_type"] = "bad"
            elif "осв" in txt_lower:
                res["mark"] = "ОСВ"
            else:
                # [FIX 1] Нестандартные оценки: выводим как есть
                res["mark"] = txt.capitalize()[:20] 
                
    return res

def process_pdf(content: bytes, course: int) -> List[str]:
    imgs = []
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            total = len(doc)
            start, end = 0, total
            if 0 < course < 6:
                start = (course - 1) * 2
                end = min(start + 2, total)
            if total <= 2: start, end = 0, total

            for i in range(start, end):
                pix = doc[i].get_pixmap(matrix=fitz.Matrix(3, 3))
                imgs.append(base64.b64encode(pix.tobytes("jpg")).decode('utf-8'))
    except: pass
    return imgs

def get_fir_pdf(spec_name: str, course: int) -> List[str]:
    filename = ""
    for key, val in SPECIALTY_MAP.items():
        if key in spec_name.lower():
            filename = val
            break
    if not filename: return []
    try:
        url = f"https://fir.bsu.by/images/timetable/{filename}"
        r = requests.get(url, verify=False, timeout=10)
        if r.status_code == 200: return process_pdf(r.content, course)
    except: pass
    return []

# --- ENDPOINTS ---

@app.post("/login")
def login(data: LoginRequest):
    # [FIX 2] Авто-ретрай (3 попытки) при входе
    for attempt in range(3):
        logger.info(f"Login attempt {attempt + 1}")
        s = requests.Session()
        s.proxies.update(PROXIES)
        
        try:
            r1 = s.get("https://student.bsu.by/login.aspx", headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r1.text, 'html.parser')
            
            viewstate = soup.find("input", {"id": "__VIEWSTATE"})
            eventval = soup.find("input", {"id": "__EVENTVALIDATION"})
            
            if not viewstate or not eventval:
                logger.warning("No ViewState found on login page")
                continue

            img = soup.find("img", {"id": re.compile("CaptchaImage")})
            if not img: raise Exception("No captcha")
            
            cap_url = "https://student.bsu.by" + img['src']
            cap_res = s.get(cap_url, headers=HEADERS)
            code = ocr.classification(cap_res.content)
            
            payload = {
                "__VIEWSTATE": viewstate.get("value", ""),
                "__EVENTVALIDATION": eventval.get("value", ""),
                "ctl00$MainContent$Username": data.username,
                "ctl00$MainContent$Password": data.password,
                "ctl00$MainContent$CaptchaCode": code,
                "ctl00$MainContent$LoginButton": "Войти"
            }
            
            r2 = s.post("https://student.bsu.by/login.aspx", data=payload, headers=HEADERS, allow_redirects=False)
            
            # Успешный вход = редирект или наличие кнопки выхода
            if r2.status_code == 302 or "Logout.aspx" in r2.text:
                return {"status": "ok", "cookies": s.cookies.get_dict()}
            
        except Exception as e:
            logger.error(f"Attempt {attempt} failed: {e}")
            time.sleep(1) # Пауза перед ретраем
            
    raise HTTPException(401, "Ошибка входа. Проверьте логин/пароль.")

@app.post("/schedule")
def get_data(data: ScheduleRequest):
    # [FIX 2] Ретрай для получения данных (защита от разрывов сети)
    for attempt in range(2):
        try:
            return _get_data_internal(data)
        except HTTPException as he:
            raise he # Сразу пробрасываем 401
        except Exception as e:
            logger.error(f"Schedule attempt {attempt} failed: {e}")
            if attempt == 1: raise HTTPException(500, "Ошибка связи с БГУ")
            time.sleep(1)

def _get_data_internal(data: ScheduleRequest):
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.cookies.update(data.cookies)
    
    resp = {
        "fio": "Не найдено", 
        "current_session": "", 
        "subjects": [], 
        "schedule_images": [], 
        "photo_base64": None, 
        "average_grade": "-", 
        "specialty": "", 
        "news": [],
        "semesters": [], 
        "current_semester_id": ""
    }
    course = 0

    url = "https://student.bsu.by/PersonalCabinet/StudProgress"
    r = s.get(url, headers=HEADERS, timeout=15)
    
    # [FIX: CRITICAL] Если редирект на логин - сразу 401
    if "login.aspx" in r.url.lower() or "login.aspx" in r.text.lower(): 
        raise HTTPException(401, "Session expired")
        
    soup = BeautifulSoup(r.text, 'html.parser')

    # [FIX 3] Обработка переключения семестров
    sem_select = soup.find("select", id=re.compile("ddlSemestr")) or soup.find("select", id=re.compile("ddlKurs"))
    if sem_select:
        for opt in sem_select.find_all("option"):
            resp["semesters"].append({
                "id": opt.get("value"),
                "name": opt.get_text(strip=True),
                "selected": opt.get("selected") is not None
            })
            if opt.get("selected"): resp["current_semester_id"] = opt.get("value")

    # Если попросили другой семестр
    if data.period_id and sem_select and data.period_id != resp["current_semester_id"]:
        try:
            vs = soup.find("input", {"id": "__VIEWSTATE"}).get("value", "")
            ev = soup.find("input", {"id": "__EVENTVALIDATION"}).get("value", "")
            sel_name = sem_select.get("name")
            
            payload = {
                "__VIEWSTATE": vs,
                "__EVENTVALIDATION": ev,
                "__EVENTTARGET": sel_name,
                sel_name: data.period_id
            }
            r_switch = s.post(url, data=payload, headers=HEADERS)
            soup = BeautifulSoup(r_switch.text, 'html.parser')
            resp["current_semester_id"] = data.period_id
        except: pass

    # 1. ФИО - Главный маркер валидности страницы
    fio_tag = soup.find("span", id=re.compile(r"lbFIO1$"))
    if fio_tag:
        resp["fio"] = clean_text(fio_tag.text)
    else:
        # [FIX: CRITICAL] Если на странице нет ФИО, значит это не ЛК.
        # Это предотвращает "Ничего не найдено".
        raise HTTPException(401, "Invalid session (FIO not found)")

    # 2. Балл
    ball = soup.find("span", id=re.compile(r"lbStudBall$"))
    if ball:
        m = re.search(r'(\d+[,.]\d+)', ball.text)
        if m: resp["average_grade"] = m.group(1).replace(",", ".")

    # 3. Курс
    kurs = soup.find("span", id=re.compile(r"lbStudKurs$"))
    if kurs:
        txt = clean_text(kurs.text)
        cm = re.search(r'(\d+)\s*курс', txt)
        if cm: course = int(cm.group(1))
        
        if "специальность:" in txt.lower():
            resp["specialty"] = txt.lower().split("специальность:")[1].split(",")[0].strip().capitalize()
        else:
            resp["specialty"] = txt

    # 4. Фото
    if soup.find("img", id=re.compile(r"imgStudPhoto$")):
        try:
            ri = s.get("https://student.bsu.by/Photo/Photo.aspx", headers=HEADERS)
            if ri.status_code == 200:
                resp["photo_base64"] = base64.b64encode(ri.content).decode('utf-8')
        except: pass

    # 5. Таблица
    table = None
    for t in soup.find_all("table"):
        if "№ п/п" in t.text:
            table = t
            break
    
    if table:
        if table.find("tr"): resp["current_session"] = clean_text(table.find("tr").text)
        for row in table.find_all("tr"):
            name_cell = row.find("td", class_=re.compile("styleLesson"))
            if name_cell:
                nm = clean_text(name_cell.get_text(separator=" ")).replace("Дисциплины по выбору студента:", "").strip()
                if len(nm) < 3 or "предмет" in nm.lower(): continue
                
                cols = row.find_all("td")
                grade = parse_grade(cols)
                
                hm = {}
                # Логика часов (упрощенно)
                titles = ["lectures", "practice", "labs", "seminars", "electives", "ksr"]
                ti = 0
                for c in cols:
                    if "styleHours" in safe_get_attr(c, "class"):
                        if ti < len(titles) and c.text.strip().isdigit():
                            hm[titles[ti]] = int(c.text.strip())
                        ti += 1
                        
                resp["subjects"].append({
                    "name": nm, "hours": hm, "mark": grade["mark"], "color": grade["color_type"]
                })

    # 6. Расписание
    pdf_found = False
    for a in soup.find_all("a", href=True):
        href = str(a.get('href', ''))
        if ".pdf" in href.lower():
            u = "https://student.bsu.by" + href if href.startswith("/") else href
            try:
                rp = s.get(u, headers=HEADERS)
                if rp.status_code == 200:
                    resp["schedule_images"] = process_pdf(rp.content, course)
                    pdf_found = True
                    break
            except: pass
            
    if not pdf_found and resp["specialty"]:
        resp["schedule_images"] = get_fir_pdf(str(resp["specialty"]), course)

    # 7. Новости
    try:
        rn = s.get("https://student.bsu.by/PersonalCabinet/News", headers=HEADERS)
        sn = BeautifulSoup(rn.text, 'html.parser')
        for a in sn.find_all("a"):
            if "Подробнее" in a.get_text():
                p = a.parent
                if p:
                    full = clean_text(p.text)
                    dm = re.search(r'\d{2}\.\d{2}\.\d{4}', full)
                    dt = dm.group(0) if dm else ""
                    cnt = full.replace("Подробнее...", "").replace(dt, "").strip()
                    if cnt: resp["news"].append({"date": dt, "title": cnt[:60]+"...", "content": cnt})
    except: pass

    return {"status": "ok", "data": resp}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)