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
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
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
            # Ищем цифру
            m = re.search(r'^(\d+)', txt)
            if m:
                res["mark"] = m.group(1)
                try:
                    v = int(res["mark"])
                    # Схема БГЭУ/БГУ (стипендия с 6.0)
                    if v < 4: res["color_type"] = "bad"
                    elif v < 6: res["color_type"] = "bad"     # 4-5: Красный (без стипендии)
                    elif v < 7: res["color_type"] = "neutral" # 6: Желтый (обычная)
                    elif v < 9: res["color_type"] = "good"    # 7-8: Зеленоватый (+%)
                    else: res["color_type"] = "excellent"     # 9-10: Ярко-зеленый (макс)
                except: pass
            else:
                # Если текст (напр. "неявка")
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
                # Нестандартные оценки: выводим как есть
                res["mark"] = txt.capitalize()[:25]
                
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
    # Авто-ретрай
    for attempt in range(3):
        logger.info(f"Login attempt {attempt + 1}")
        s = requests.Session()
        s.proxies.update(PROXIES)
        
        try:
            r1 = s.get("https://student.bsu.by/login.aspx", headers=HEADERS, timeout=15)
            soup = BeautifulSoup(r1.text, 'html.parser')
            
            # Собираем скрытые поля
            viewstate = soup.find("input", {"id": "__VIEWSTATE"})
            eventval = soup.find("input", {"id": "__EVENTVALIDATION"})
            
            if not viewstate or not eventval:
                logger.warning("No ViewState found.")
                continue

            # --- ПОИСК КАПЧИ ПО ТВОЕМУ HTML ---
            # Ищем внутри <td> с конкретным ID из твоего файла
            img = None
            td_captcha = soup.find("td", {"id": "ctl00_ContentPlaceHolder0_TableCell1"})
            if td_captcha:
                img = td_captcha.find("img")
            
            if not img:
                # Резервный поиск
                img = soup.find("img", src=re.compile("Captcha", re.I))
            
            if not img: raise Exception("Captcha not found")
            
            # Формируем ссылку на картинку
            src = img['src']
            if not src.startswith("http"):
                src = "https://student.bsu.by" + src if src.startswith("/") else "https://student.bsu.by/" + src
                
            # Качаем и разгадываем
            cap_res = s.get(src, headers=HEADERS)
            raw_code = ocr.classification(cap_res.content)
            code = re.sub(r'\D', '', raw_code.lower().replace('o', '0').replace('l', '1').replace('z', '2'))
            
            logger.info(f"Solved code: {code} (raw: {raw_code})")
            
            # --- ПРАВИЛЬНЫЙ PAYLOAD (ИЗ ТВОЕГО HTML) ---
            payload = {
                "__VIEWSTATE": viewstate.get("value", ""),
                "__EVENTVALIDATION": eventval.get("value", ""),
                # ВОТ ОНИ, ПРАВИЛЬНЫЕ ИМЕНА ПОЛЕЙ:
                "ctl00$ContentPlaceHolder0$txtUserLogin": data.username,
                "ctl00$ContentPlaceHolder0$txtUserPassword": data.password,
                "ctl00$ContentPlaceHolder0$txtCapture": code,
                "ctl00$ContentPlaceHolder0$btnLogon": "Войти"
            }
            
            # Заголовки для имитации браузера
            post_headers = HEADERS.copy()
            post_headers["Origin"] = "https://student.bsu.by"
            post_headers["Referer"] = "https://student.bsu.by/login.aspx"
            
            r2 = s.post("https://student.bsu.by/login.aspx", data=payload, headers=post_headers, allow_redirects=False)
            
            # Проверка входа
            if r2.status_code == 302 or "Logout.aspx" in r2.text:
                return {"status": "ok", "cookies": s.cookies.get_dict()}
            
        except Exception as e:
            logger.error(f"Attempt {attempt} error: {e}")
            time.sleep(1)
            
    raise HTTPException(401, "Login failed. Check credentials.")

@app.post("/schedule")
def get_data(data: ScheduleRequest):
    for attempt in range(2):
        try:
            return _get_data_internal(data)
        except HTTPException as he: raise he
        except Exception as e:
            logger.error(f"Schedule error: {e}")
            if attempt == 1: raise HTTPException(500, "BSU connection error")
            time.sleep(1)

def _get_data_internal(data: ScheduleRequest):
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.cookies.update(data.cookies)
    
    resp = {
        "fio": "Загрузка...", "current_session": "", "subjects": [], 
        "schedule_images": [], "photo_base64": None, "average_grade": "-", 
        "specialty": "", "news": [], "semesters": [], "current_semester_id": ""
    }
    course = 0

    url = "https://student.bsu.by/PersonalCabinet/StudProgress"
    r = s.get(url, headers=HEADERS, timeout=15)
    
    # Если выкинуло на логин
    if "login.aspx" in r.url.lower(): 
        raise HTTPException(401, "Session expired")
        
    soup = BeautifulSoup(r.text, 'html.parser')

    # Парсинг семестров
    # Ищем селектор семестра (имя может отличаться, ищем по смыслу)
    sem_select = soup.find("select", id=re.compile("ddlSemestr")) or soup.find("select", id=re.compile("ddlKurs"))
    if sem_select:
        for opt in sem_select.find_all("option"):
            resp["semesters"].append({
                "id": opt.get("value"),
                "name": opt.get_text(strip=True),
                "selected": opt.get("selected") is not None
            })
            if opt.get("selected"): resp["current_semester_id"] = opt.get("value")

    # Смена семестра
    if data.period_id and sem_select and data.period_id != resp["current_semester_id"]:
        try:
            payload = {}
            for inp in soup.find_all("input", type="hidden"):
                payload[inp.get("name")] = inp.get("value", "")
            
            payload["__EVENTTARGET"] = sem_select.get("name")
            payload[sem_select.get("name")] = data.period_id
            
            r = s.post(url, data=payload, headers=HEADERS)
            soup = BeautifulSoup(r.text, 'html.parser')
            resp["current_semester_id"] = data.period_id
        except: pass

    # ФИО
    fio_tag = soup.find("span", id=re.compile(r"lbFIO1$"))
    if fio_tag: resp["fio"] = clean_text(fio_tag.text)
    else: raise HTTPException(401, "Invalid session (No FIO)")

    # Остальные данные
    ball = soup.find("span", id=re.compile(r"lbStudBall$"))
    if ball:
        m = re.search(r'(\d+[,.]\d+)', ball.text)
        if m: resp["average_grade"] = m.group(1).replace(",", ".")

    kurs = soup.find("span", id=re.compile(r"lbStudKurs$"))
    if kurs:
        txt = clean_text(kurs.text)
        cm = re.search(r'(\d+)\s*курс', txt)
        if cm: course = int(cm.group(1))
        if "специальность:" in txt.lower():
            resp["specialty"] = txt.lower().split("специальность:")[1].split(",")[0].strip().capitalize()
        else:
            resp["specialty"] = txt

    if soup.find("img", id=re.compile(r"imgStudPhoto$")):
        try:
            ri = s.get("https://student.bsu.by/Photo/Photo.aspx", headers=HEADERS)
            if ri.status_code == 200:
                resp["photo_base64"] = base64.b64encode(ri.content).decode('utf-8')
        except: pass

    # Таблица с оценками
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
                if len(nm) < 3 or "предмет" in nm.lower() or "название дисциплины" in nm.lower(): continue
                
                cols = row.find_all("td")
                grade = parse_grade(cols)
                
                hm = {}
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

    # Расписание (PDF)
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

    # Новости
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