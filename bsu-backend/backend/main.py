from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import ddddocr
import uvicorn
from bs4 import BeautifulSoup
import base64
import re
import fitz

app = FastAPI()
ocr = ddddocr.DdddOcr(show_ad=False)

class LoginRequest(BaseModel):
    username: str
    password: str

class ScheduleRequest(BaseModel):
    cookies: dict

SPECIALTY_MAP = {
    "международные отношения": "IR_timetable.pdf",
    "мировая экономика": "WE_timetable.pdf",
    "международное право": "IL_timetable.pdf",
    "таможенное дело": "CA_timetable.pdf",
    "востоковедение": "V_timetable.pdf",
    "международная конфликтология": "IC_timetable.pdf",
    "международная логистика": "ILOG_timetable.pdf",
    "африканистика": "AF_timetable.pdf"
}

@app.post("/login")
def login(data: LoginRequest):
    print(f"\n🔹 Вход пользователя: {data.username}")
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://student.bsu.by/login.aspx"
    }

    try:
        r_page = session.get("https://student.bsu.by/login.aspx", headers=headers)
        soup = BeautifulSoup(r_page.text, 'html.parser')
        payload = {}
        for inp in soup.find_all('input', type='hidden'):
            if inp.get('name'):
                payload[inp.get('name')] = inp.get('value', '')

        print("   🤖 Решаю капчу...")
        r_captcha = session.get("https://student.bsu.by/Captcha/CaptchaImage.aspx", headers=headers)
        captcha_code = ocr.classification(r_captcha.content)

        payload.update({
            'ctl00$ContentPlaceHolder0$txtUserLogin': data.username,
            'ctl00$ContentPlaceHolder0$txtUserPassword': data.password,
            'ctl00$ContentPlaceHolder0$txtCapture': captcha_code,
            'ctl00$ContentPlaceHolder0$btnLogon': 'Войти'
        })

        print("   🚀 Отправляю данные...")
        r_login = session.post("https://student.bsu.by/login.aspx", data=payload, headers=headers, allow_redirects=False)

        if r_login.status_code == 302 or (r_login.status_code == 200 and "Выход" in r_login.text):
            print("   ✅ УСПЕХ! Куки получены.")
            return {"status": "success", "cookies": session.cookies.get_dict()}
        else:
            print("   ❌ Ошибка входа.")
            raise HTTPException(status_code=401, detail="Неверный логин или капча")
    except Exception as e:
        print(f"🔥 Ошибка: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/schedule")
def get_data(data: ScheduleRequest):
    print("\n🔹 Запрос данных кабинета...")
    session = requests.Session()
    session.cookies.update(data.cookies)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    response_data = {
        "fio": "Студент БГУ",
        "photo_base64": None,
        "grade_val": 0.0,
        "grade_text": "Нет данных",
        "news": [],
        "schedule_images": []
    }

    try:
        print("   📰 Качаю новости...")
        r_news = session.get("https://student.bsu.by/PersonalCabinet/News", headers=headers)
        soup_news = BeautifulSoup(r_news.text, 'html.parser')

        fio_span = soup_news.find("span", id=lambda x: x and x.endswith("lbFIO1"))
        if fio_span: response_data["fio"] = fio_span.text.strip()

        news_items = soup_news.find_all("h2", align="left")
        for h2 in news_items:
            try:
                link_tag = h2.find("a")
                if link_tag:
                    title = link_tag.text.strip()
                    raw_link = link_tag.get("href", "")
                    if raw_link.startswith("/"): full_link = "https://student.bsu.by" + raw_link
                    else: full_link = raw_link
                    desc = ""
                    next_p = h2.find_next_sibling("p")
                    if next_p: desc = next_p.text.strip()
                    if title:
                        response_data["news"].append({"title": title, "desc": desc, "link": full_link})
            except: continue

        print("   📸 Качаю фото...")
        r_photo = session.get("https://student.bsu.by/Photo/Photo.aspx", headers=headers)
        if r_photo.status_code == 200:
            response_data["photo_base64"] = base64.b64encode(r_photo.content).decode('utf-8')

        print("   🎓 Анализирую профиль...")
        r_grade = session.get("https://student.bsu.by/PersonalCabinet/StudProgress", headers=headers)
        soup_grade = BeautifulSoup(r_grade.text, 'html.parser')

        grade_span = soup_grade.find("span", id=lambda x: x and x.endswith("lbStudBall"))
        if grade_span:
            text = grade_span.text.strip()
            response_data["grade_text"] = " ".join(text.split())
            match = re.search(r'(\d+[\.,]\d+)', text)
            if match:
                try: response_data["grade_val"] = float(match.group(1).replace(',', '.'))
                except: pass

        course_span = soup_grade.find("span", id=lambda x: x and x.endswith("lbStudKurs"))
        user_course = 1
        pdf_filename = "CA_timetable.pdf"

        if course_span:
            info_text = course_span.text.strip().lower()
            print(f"      ℹ️ Инфо: {info_text}")
            course_match = re.search(r'(\d+)\s*курс', info_text)
            if course_match: user_course = int(course_match.group(1))

            for part in info_text.split(','):
                if "специальность" in part:
                    spec_name = part.replace("специальность:", "").strip()
                    if spec_name in SPECIALTY_MAP:
                        pdf_filename = SPECIALTY_MAP[spec_name]
                        print(f"      ✅ Найдена специальность: {spec_name} -> {pdf_filename}")

        print(f"   📅 Качаю PDF ({pdf_filename}, Курс {user_course})...")
        pdf_url = f"https://fir.bsu.by/images/timetable/{pdf_filename}"
        r_pdf = requests.get(pdf_url, verify=False)

        if r_pdf.status_code == 200:
            with fitz.open(stream=r_pdf.content, filetype="pdf") as doc:
                start_page = (user_course - 1) * 2
                pages_to_read = [start_page, start_page + 1]

                for page_num in pages_to_read:
                    if page_num < len(doc):
                        page = doc.load_page(page_num)

                        pix = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5))

                        img_data = pix.tobytes("jpg")

                        b64_img = base64.b64encode(img_data).decode('utf-8')
                        response_data["schedule_images"].append(b64_img)
        else:
            print("      ❌ Ошибка скачивания PDF")

        return {"status": "ok", "data": response_data}

    except Exception as e:
        print(f"🔥 Ошибка: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)