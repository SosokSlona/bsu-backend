from pydantic import BaseModel
from typing import List, Dict, Optional, Any

# --- НОВЫЕ МОДЕЛИ (для парсера pdfplumber) ---

class LessonItem(BaseModel):
    subject: str          # Название предмета
    type: str             # (Лек) / (Прак)
    teacher: str          # Иванов И.И.
    room: str             # 501
    time_start: str       # 09:00
    time_end: str         # 10:20

class DaySchedule(BaseModel):
    day_name: str
    lessons: List[LessonItem]

# Ответ: { "Группа 13": [Пн, Вт...], "Группа 14": [...] }
class ParsedScheduleResponse(BaseModel):
    groups: Dict[str, List[DaySchedule]]


# --- СТАРЫЕ МОДЕЛИ (для main.py / Login) ---
# Именно их не хватало, из-за чего ошибка ImportError

class LoginRequest(BaseModel):
    username: str
    password: str

class ScheduleRequest(BaseModel):
    cookies: Dict[str, str]
    period_id: Optional[str] = None