from pydantic import BaseModel
from typing import Optional

class Workout(BaseModel):
    # Mandatory fields
    name: str
    sport: str
    type: str # e.g., Z1, Z2, Z3, UT1, UT2, AT, TR, AN, etc.
    date: str
    duration: int

    # Optional fields
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    heart_rate: Optional[int] = None
    distance: Optional[float] = None
    power: Optional[int] = None
    calories_burned: Optional[int] = None
    intensity: Optional[str] = None # e.g., low, medium, high
    perceived_exertion: Optional[int] = None # RPE scale 1-10
    location: Optional[str] = None
    notes: Optional[str] = None


