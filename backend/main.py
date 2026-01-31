import logging
import re
import base64
import requests
import uvicorn
import ddddocr
import fitz 
import os
import json
import hashlib
import asyncio
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from bs4 import BeautifulSoup
from typing import List, Dict, Any, Optional, Set
from datetime import datetime, timedelta

from models import LoginRequest, ScheduleRequest, ParsedScheduleResponse
from schedule_parser import parse_schedule_pdf

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BSU_Backend")

app = FastAPI()
ocr = ddddocr.DdddOcr(show_ad=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROXIES = {
    "http": "socks5://localhost:1080",
    "https": "socks5://localhost:1080"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
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

# --- КЕШИРОВАНИЕ ---
CACHE_DIR = "schedule_cache"
CACHE_VERSION = "v0" 
if not os.path.exists(CACHE_DIR): os.makedirs(CACHE_DIR)

ACTIVE_SCHEDULES: Set[tuple] = set()

def get_cache_filename(pdf_url: str, course: int) -> str:
    unique_str = f"{pdf_url}_course_{course}_{CACHE_VERSION}"
    hash_obj = hashlib.md5(unique_str.encode())
    return os.path.join(CACHE_DIR, f"{hash_obj.hexdigest()}.json")

def load_from_cache(filename: str) -> Optional[ParsedScheduleResponse]:
    if not os.path.exists(filename): return None
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return ParsedScheduleResponse(**json.load(f))
    except: return None

def save_to_cache(filename: str, data: ParsedScheduleResponse):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(data.json())
    except: pass

# --- ФОНОВОЕ ОБНОВЛЕНИЕ ---
async def refresh_schedules_task():
    while True:
        logger.info(f"🔄 Auto-Refresh: {len(ACTIVE_SCHEDULES)} schedules")
        for pdf_url, course in list(ACTIVE_SCHEDULES):
            try:
                s = requests.Session()
                s.proxies.update(PROXIES)
                resp = s.get(pdf_url, headers=HEADERS, verify=False, timeout=60)
                if resp.status_code == 200:
                    data = await asyncio.to_thread(parse_schedule_pdf, resp.content, course)
                    if data.groups:
                        save_to_cache(get_cache_filename(pdf_url, course), data)
                        logger.info(f"✅ Refreshed: {pdf_url}")
            except Exception as e:
                logger.error(f"❌ Refresh failed: {e}")
            await asyncio.sleep(10)
        await asyncio.sleep(7200)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_schedules_task())

# --- ПАРСЕР ОЦЕНОК (ТВОЙ КОД) ---

def clean_text(text: Any) -> str:
    """Удаляет лишние пробелы и неразрывные пробелы из текста."""
    if not text: return ""
    return re.sub(r'\s+', ' ', str(text).replace('\xa0', ' ').strip())

def safe_get_attr(element: Any, attr: str) -> str:
    """Безопасно получает атрибут тега (класс, id и т.д.)."""
    if not element: return ""
    val = element.get(attr)
    if isinstance(val, list): return " ".join(val)
    return str(val) if val else ""

def parse_grade_row(cols: List[Any]) -> Dict[str, str]:
    """Парсит строку таблицы успеваемости (Sexy version)."""
    res = {"mark": "", "color_type": "neutral"}
    exam_cell = None
    credit_cell = None
    
    for c in cols:
        cls = safe_get_attr(c, "class")
        if "styleExamBody" in cls: exam_cell = c
        if "styleZachBody" in cls: credit_cell = c
    
    # 1. Экзамены (Цифры)
    if exam_cell:
        raw_text = clean_text(exam_cell.get_text()) or safe_get_attr(exam_cell, "title")
        if raw_text:
            match = re.search(r'^(\d+)', raw_text)
            if match:
                res["mark"] = match.group(1)
                try:
                    val = int(res["mark"])
                    if val < 4: res["color_type"] = "bad"
                    elif val < 6: res["color_type"] = "bad"
                    elif val < 7: res["color_type"] = "neutral"
                    elif val < 9: res["color_type"] = "good"
                    else: res["color_type"] = "excellent"
                except ValueError: pass
            else:
                res["mark"] = raw_text[:15]
                res["color_type"] = "bad"
            return res

    # 2. Зачеты (Текст)
    if credit_cell:
        raw_text = clean_text(credit_cell.get_text())
        if raw_text:
            text_lower = raw_text.lower()
            if any(x in text_lower for x in ["зачтено", "+", "зачёт"]):
                res["mark"] = "Зачет"
                res["color_type"] = "good"
            elif any(x in text_lower for x in ["не зачтено", "незач", "-"]):
                res["mark"] = "Незач"
                res["color_type"] = "bad"
            else:
                clean_mark = raw_text.split('(')[0].strip()
                res["mark"] = clean_mark.capitalize()[:20]
                res["color_type"] = "neutral"
                if "осв" in text_lower:
                    res["mark"] = "ОСВ"
    return res

# --- ENDPOINTS ---

@app.post("/login")
def login(data: LoginRequest):
    for attempt in range(3):
        s = requests.Session()
        s.proxies.update(PROXIES)
        try:
            r1 = s.get("https://student.bsu.by/login.aspx", headers=HEADERS, timeout=10)
            soup = BeautifulSoup(r1.text, 'html.parser')
            viewstate = soup.find("input", {"id": "__VIEWSTATE"})
            eventval = soup.find("input", {"id": "__EVENTVALIDATION"})
            
            img = soup.find("img", src=re.compile("CaptchaImage", re.I)) or soup.find("img", src=re.compile("Captcha", re.I))
            if not img: raise Exception("No captcha")
            
            src = "https://student.bsu.by" + img['src'] if img['src'].startswith("/") else img['src']
            cap = s.get(src, headers=HEADERS)
            code = re.sub(r'\D', '', ocr.classification(cap.content).lower().replace('o','0').replace('l','1'))
            
            payload = {
                "__VIEWSTATE": viewstate.get("value", ""),
                "__EVENTVALIDATION": eventval.get("value", ""),
                "ctl00$ContentPlaceHolder0$txtUserLogin": data.username,
                "ctl00$ContentPlaceHolder0$txtUserPassword": data.password,
                "ctl00$ContentPlaceHolder0$txtCapture": code,
                "ctl00$ContentPlaceHolder0$btnLogon": "Войти"
            }
            r2 = s.post("https://student.bsu.by/login.aspx", data=payload, headers=post_headers, allow_redirects=False)
            
            if r2.status_code == 302 or "Logout.aspx" in r2.text:
                return {"status": "ok", "cookies": s.cookies.get_dict()}
        except Exception: time.sleep(1)
    raise HTTPException(401, "Login failed")

# Эндпоинт для быстрой инфы (шапка приложения)
@app.post("/user-info")
def get_user_info(data: ScheduleRequest):
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.cookies.update(data.cookies)
    
    resp = {"fio": "", "average_grade": "-", "specialty": ""}
    
    try:
        r = s.get("https://student.bsu.by/PersonalCabinet/StudProgress", headers=HEADERS, timeout=10)
        if "login.aspx" in r.url.lower(): raise HTTPException(401, "Session expired")
        soup = BeautifulSoup(r.text, 'html.parser')
        
        fio = soup.find("span", id=re.compile(r"lbFIO1$"))
        if fio: resp["fio"] = clean_text(fio.text)
        
        ball = soup.find("span", id=re.compile(r"lbStudBall$"))
        if ball:
            m = re.search(r'(\d+[,.]\d+)', ball.text)
            if m: resp["average_grade"] = m.group(1).replace(",", ".")
            
        return resp
    except Exception as e:
        logger.error(f"UserInfo error: {e}")
        raise HTTPException(500, str(e))

# Эндпоинт для ОЦЕНОК и НОВОСТЕЙ (Тот самый, который был 404)
@app.post("/schedule")
def get_grades_and_news(data: ScheduleRequest):
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.cookies.update(data.cookies)
    
    resp = {
        "status": "ok",
        "data": {
            "subjects": [], 
            "news": []
        }
    }
    
    try:
        # 1. Забираем Оценки
        url = "https://student.bsu.by/PersonalCabinet/StudProgress"
        r = s.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')

        # Ищем таблицу с оценками
        table = None
        for t in soup.find_all("table"):
            if "№ п/п" in t.text: table = t; break
            
        if table:
            for row in table.find_all("tr"):
                name_cell = row.find("td", class_=re.compile("styleLesson"))
                if name_cell:
                    nm = clean_text(name_cell.get_text(separator=" ")).replace("Дисциплины по выбору студента:", "").strip()
                    if len(nm) < 3 or "предмет" in nm.lower(): continue
                    
                    cols = row.find_all("td")
                    # ИСПОЛЬЗУЕМ ТВОЙ ПАРСЕР
                    grade_data = parse_grade_row(cols)
                    
                    # Часы (лекции/практики)
                    hm = {}
                    titles = ["lectures", "practice", "labs", "seminars", "electives", "ksr"]
                    ti = 0
                    for c in cols:
                        if "styleHours" in safe_get_attr(c, "class"):
                            if ti < len(titles) and c.text.strip().isdigit(): 
                                hm[titles[ti]] = int(c.text.strip())
                            ti += 1

                    resp["data"]["subjects"].append({
                        "name": nm, 
                        "hours": hm, 
                        "mark": grade_data["mark"], 
                        "color": grade_data["color_type"]
                    })

        # 2. Забираем Новости (бонус)
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
                        if cnt: resp["data"]["news"].append({"date": dt, "title": cnt[:60]+"...", "content": cnt})
        except: pass
        
        return resp

    except Exception as e:
        logger.error(f"Grades Error: {e}")
        raise HTTPException(500, str(e))

# Эндпоинт для PDF Расписания (OCR)
@app.post("/schedule/parse", response_model=ParsedScheduleResponse)
async def parse_schedule(data: ScheduleRequest):
    s = requests.Session()
    s.proxies.update(PROXIES)
    s.cookies.update(data.cookies)
    
    course = 1
    try:
        r = s.get("https://student.bsu.by/PersonalCabinet/StudProgress", headers=HEADERS, timeout=10)
        soup = BeautifulSoup(r.text, 'html.parser')
        
        kurs_span = soup.find("span", id=re.compile(r"lbStudKurs$"))
        if kurs_span:
            m = re.search(r'(\d+)\s*курс', kurs_span.text)
            if m: course = int(m.group(1))

        pdf_url = None
        for a in soup.find_all("a", href=True):
            if ".pdf" in str(a.get('href')).lower():
                pdf_url = "https://student.bsu.by" + a.get('href') if a.get('href').startswith("/") else a.get('href')
                break
        
        if not pdf_url:
            if kurs_span:
                txt = kurs_span.text.lower()
                for k, v in SPECIALTY_MAP.items():
                    if k in txt: pdf_url = f"https://fir.bsu.by/images/timetable/{v}"; break
        
        if not pdf_url: raise HTTPException(404, "No PDF found")

        ACTIVE_SCHEDULES.add((pdf_url, course))
        cache_file = get_cache_filename(pdf_url, course)
        
        cached = load_from_cache(cache_file)
        if cached: return cached

        logger.info(f"Downloading PDF: {pdf_url}")
        pdf_resp = s.get(pdf_url, headers=HEADERS, verify=False)
        
        parsed = await asyncio.to_thread(parse_schedule_pdf, pdf_resp.content, course)
        
        if parsed.groups:
            save_to_cache(cache_file, parsed)
        
        return parsed

    except Exception as e:
        logger.error(f"Parse error: {e}")
        raise HTTPException(500, str(e))

# Нужно для логина, если где-то используется напрямую
post_headers = HEADERS.copy()
post_headers.update({"Origin": "https://student.bsu.by", "Referer": "https://student.bsu.by/login.aspx"})

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)