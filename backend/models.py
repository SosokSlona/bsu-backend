from pydantic import BaseModel
from typing import List, Dict, Optional

class LessonItem(BaseModel):
    subject: str
    type: str
    teacher: str
    room: str
    time_start: str
    time_end: str
    subgroup: Optional[str] = None

class DaySchedule(BaseModel):
    day_name: str
    lessons: List[LessonItem]

class ParsedScheduleResponse(BaseModel):
    groups: Dict[str, List[DaySchedule]]

class LoginRequest(BaseModel):
    username: str
    password: str

class ScheduleRequest(BaseModel):
    cookies: Dict[str, str]
    period_id: Optional[str] = None