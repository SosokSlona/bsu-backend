from pydantic import BaseModel
from typing import List, Dict, Optional

# --- МОДЕЛИ ДАННЫХ ---

class LessonItem(BaseModel):
    subject: str          # Название предмета (очищенное)
    type: str             # (Прак) / (Лек)
    teacher: str          # Фамилия И.О.
    room: str             # 501
    time_start: str       # 09:00
    time_end: str         # 10:20
    subgroup: Optional[str] = None # "Английский" или "1 подгруппа"

class DaySchedule(BaseModel):
    day_name: str
    lessons: List[LessonItem]

class ParsedScheduleResponse(BaseModel):
    groups: Dict[str, List[DaySchedule]]

# --- МОДЕЛИ ЗАПРОСОВ ---
class LoginRequest(BaseModel):
    username: str
    password: str

class ScheduleRequest(BaseModel):
    cookies: Dict[str, str]
    period_id: Optional[str] = None