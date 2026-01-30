from pydantic import BaseModel
from typing import List, Dict, Optional

class LessonItem(BaseModel):
    subject: str          # Математика
    type: str             # (Лек) или (Прак)
    teacher: str          # Иванов И.И.
    room: str             # 501
    time_start: str       # 09:00
    time_end: str         # 10:20

class DaySchedule(BaseModel):
    day_name: str
    lessons: List[LessonItem]

# Ответ парсера: Ключ - название группы (из шапки таблицы), Значение - расписание недели
class ParsedScheduleResponse(BaseModel):
    groups: Dict[str, List[DaySchedule]]