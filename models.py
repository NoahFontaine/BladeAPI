from pydantic import BaseModel, EmailStr
from typing import Optional


class User(BaseModel):
    name: str
    email: str
    username: Optional[str] = None
    password: Optional[str] = None
    squad: Optional[str] = None # e.g., M1, W1, M2, W2, etc.
    age: Optional[int] = None
    weight: Optional[float] = None
    height: Optional[float] = None


class Workout(BaseModel):
    # Mandatory fields
    user: User
    sport: str
    type: str # e.g., Z1, Z2, Z3, UT1, UT2, AT, TR, AN, etc.
    date: str
    duration: int

    # Optional fields
    squad: Optional[str] = None
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


class BusyEvent(BaseModel):
    # Mandatory fields
    date: str
    start_time: str
    end_time: str
    squad: str

    # Optional fields
    name: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    user: Optional[User] = None
    title: Optional[str] = None
    notes: Optional[str] = None


class GoogleSyncRequest(BaseModel):
    email: EmailStr