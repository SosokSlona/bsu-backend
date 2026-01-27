import logging
import re
import base64
import requests
import uvicorn
import ddddocr
import fitz
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from bs4 import BeautifulSoup
from typing import List, Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("BSU_Backend")

app = FastAPI()

ocr = ddddocr.DdddOcr(show_ad=False)

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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

class LoginRequest(BaseModel):
    username: str
    password: str

class ScheduleRequest(BaseModel):
    cookies: Dict[str, str]

def clean_text(text: Any) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', str(text).replace('\xa0', ' ').strip())

def safe_get_attr(element: Any, attr: str) -> str:
    if not element:
        return ""
    val = element.get(attr)
    if isinstance(val, list):
        return " ".join(val)
    return str(val) if val else ""

def parse_grade(cols: List[Any]) -> Dict[str, str]:
    res = {"mark": "", "color_type": "neutral"}
    exam = None
    credit = None

    for c in cols:
        cls = safe_get_attr(c, "class")
        if "styleExamBody" in cls:
            exam = c
        if "styleZachBody" in cls:
            credit = c

    if exam:
        txt = clean_text(exam.get_text()) or safe_get_attr(exam, "title")
        m = re.search(r'^(\d+)', txt)
        if m:
            res["mark"] = m.group(1)
            try:
                v = int(res["mark"])
                if v < 4: res["color_type"] = "bad"
                elif v < 8: res["color_type"] = "neutral"
                else: res["color_type"] = "good"
            except ValueError:
                pass
            return res

    if credit:
        txt = clean_text(credit.get_text()).lower()
        if any(x in txt for x in ["зачтено", "+"]):
            res["mark"] = "Зачет"
            res["color_type"] = "good"
        elif any(x in txt for x in ["не зачтено", "-"]):
            res["mark"] = "Незач"
            res["color_type"] = "bad"
        elif "осв" in txt:
            res["mark"] = "ОСВ"

    return res

def process_pdf(content: bytes, course: int) -> List[str]:
    imgs = []
    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            total = len(doc)
            start, end = 0, total

            if 0 < course < 6:
                start = (course - 1) * 2
                end = start + 2

                if start >= total:
                    start, end = 0, total
                else:
                    end = min(end, total)

            if total <= 2:
                start, end = 0, total

            for i in range(start, end):

                pix = doc[i].get_pixmap(matrix=fitz.Matrix(3, 3))
                imgs.append(base64.b64encode(pix.tobytes("jpg")).decode('utf-8'))
    except Exception as e:
        logger.error(f"PDF Error: {e}")
    return imgs

def get_fir_pdf(spec_name: str, course: int) -> List[str]:
    filename = ""
    spec_lower = spec_name.lower()

    for key, val in SPECIALTY_MAP.items():
        if key in spec_lower:
            filename = val
            break

    if not filename:
        return []

    try:
        url = f"https://fir.bsu.by/images/timetable/{filename}"
        r = requests.get(url, verify=False, timeout=10)
        if r.status_code == 200:
            return process_pdf(r.content, course)
    except Exception as e:
        logger.error(f"FIR PDF Error: {e}")
    return []

@app.post("/login")
def login(data: LoginRequest):
    s = requests.Session()
    try:
        r1 = s.get("https://student.bsu.by/login.aspx", headers=HEADERS)
        soup = BeautifulSoup(r1.text, 'html.parser')

        payload: Dict[str, str] = {}
        for i in soup.find_all('input', type='hidden'):
            name = i.get('name')
            value = i.get('value', '')
            if name:
                payload[str(name)] = str(value)

        r_cap = s.get("https://student.bsu.by/Captcha/CaptchaImage.aspx", headers=HEADERS)
        code = ocr.classification(r_cap.content)

        payload['ctl00$ContentPlaceHolder0$txtUserLogin'] = data.username
        payload['ctl00$ContentPlaceHolder0$txtUserPassword'] = data.password
        payload['ctl00$ContentPlaceHolder0$txtCapture'] = str(code)
        payload['ctl00$ContentPlaceHolder0$btnLogon'] = 'Войти'

        r2 = s.post("https://student.bsu.by/login.aspx", data=payload, headers=HEADERS, allow_redirects=False)

        if r2.status_code == 302:
            return {"status": "ok", "cookies": s.cookies.get_dict()}

        raise HTTPException(401, "Ошибка входа. Возможно, неверная капча или пароль.")
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(500, str(e))

@app.post("/schedule")
def get_data(data: ScheduleRequest):
    s = requests.Session()
    s.cookies.update(data.cookies)

    resp: Dict[str, Any] = {
        "fio": "Не найдено",
        "current_session": "",
        "subjects": [],
        "schedule_images": [],
        "photo_base64": None,
        "average_grade": "-",
        "specialty": "",
        "news": []
    }
    course = 0

    try:
        r = s.get("https://student.bsu.by/PersonalCabinet/StudProgress", headers=HEADERS)
        if "login.aspx" in r.url:
            raise HTTPException(401, "Session expired")

        soup = BeautifulSoup(r.text, 'html.parser')

        fio = soup.find("span", id=re.compile(r"lbFIO1$"))
        if fio: resp["fio"] = clean_text(fio.text)

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
                parts = txt.lower().split("специальность:")
                if len(parts) > 1:
                    resp["specialty"] = parts[1].split(",")[0].strip().capitalize()
            else:
                resp["specialty"] = txt

        if soup.find("img", id=re.compile(r"imgStudPhoto$")):
            try:
                ri = s.get("https://student.bsu.by/Photo/Photo.aspx", headers=HEADERS)
                if ri.status_code == 200:
                    resp["photo_base64"] = base64.b64encode(ri.content).decode('utf-8')
            except: pass

        table = None
        for t in soup.find_all("table"):
            if "№ п/п" in t.text:
                table = t
                break

        if table:
            first_row = table.find("tr")
            if first_row:
                resp["current_session"] = clean_text(first_row.text)

            for row in table.find_all("tr"):
                name_cell = row.find("td", class_=re.compile("styleLesson"))

                if name_cell:
                    raw_nm = name_cell.get_text(separator=" ", strip=True)
                    nm = clean_text(raw_nm).replace("Дисциплины по выбору студента:", "").strip()

                    if not nm or len(nm) < 3 or "предмет" in nm.lower() or "название дисциплины" in nm.lower():
                        continue

                    cols = row.find_all("td")
                    grade = parse_grade(cols)

                    hm = {}
                    titles = ["lectures", "practice", "labs", "seminars", "electives", "ksr"]
                    ti = 0
                    for c in cols:
                        if "styleHours" in safe_get_attr(c, "class"):
                            if ti < len(titles):
                                v = clean_text(c.text)
                                if v.isdigit():
                                    hm[titles[ti]] = int(v)
                            ti += 1

                    resp["subjects"].append({
                        "name": nm,
                        "hours": hm,
                        "mark": grade["mark"],
                        "color": grade["color_type"]
                    })

        pdf_found = False
        for a in soup.find_all("a", href=True):
            href_val = str(a.get('href', ''))
            if ".pdf" in href_val.lower():
                url = "https://student.bsu.by" + href_val if href_val.startswith("/") else href_val
                try:
                    rp = s.get(url, headers=HEADERS)
                    if rp.status_code == 200:
                        resp["schedule_images"] = process_pdf(rp.content, course)
                        pdf_found = True
                        break
                except: pass

        if not pdf_found and resp["specialty"]:
            resp["schedule_images"] = get_fir_pdf(str(resp["specialty"]), course)

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
                        content = full.replace("Подробнее...", "").replace(dt, "").strip()
                        if content:
                             resp["news"].append({"date": dt, "title": content[:60] + "...", "content": content})
        except Exception as e:
            logger.error(f"News parse error: {e}")

        return {"status": "ok", "data": resp}

    except Exception as e:
        logger.error(f"General error: {e}")
        raise HTTPException(500, str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)