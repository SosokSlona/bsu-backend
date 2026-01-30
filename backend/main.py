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

# --- CACHE ---
CACHE_DIR = "schedule_cache"
# Версия кеша v6 - чтобы точно сбросить весь мусор
CACHE_VERSION = "v6" 
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

# --- AUTO-REFRESH ---
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
            r2 = s.post("https://student.bsu.by/login.aspx", data=payload, headers=HEADERS, allow_redirects=False)
            
            if r2.status_code == 302 or "Logout.aspx" in r2.text:
                return {"status": "ok", "cookies": s.cookies.get_dict()}
        except Exception: time.sleep(1)
    raise HTTPException(401, "Login failed")

@app.post("/user-info")
def get_user_info(data: ScheduleRequest):
    """Быстрый запрос информации о студенте (без PDF)"""
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

@app.post("/schedule/parse", response_model=ParsedScheduleResponse)
async def parse_schedule(data: ScheduleRequest):
    """Тяжелый запрос (OCR)"""
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
        
        # 1. Кеш
        cached = load_from_cache(cache_file)
        if cached: return cached

        # 2. Скачивание и OCR (фоном)
        logger.info(f"Downloading PDF: {pdf_url}")
        pdf_resp = s.get(pdf_url, headers=HEADERS, verify=False)
        
        parsed = await asyncio.to_thread(parse_schedule_pdf, pdf_resp.content, course)
        
        if parsed.groups:
            save_to_cache(cache_file, parsed)
        
        return parsed

    except Exception as e:
        logger.error(f"Parse error: {e}")
        raise HTTPException(500, str(e))

def clean_text(text):
    return re.sub(r'\s+', ' ', str(text).replace('\xa0', ' ').strip())

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)