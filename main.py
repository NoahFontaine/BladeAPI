import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Response, Body, Query, Depends
from fastapi.responses import RedirectResponse
from fastapi.responses import RedirectResponse
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional
from contextlib import asynccontextmanager
from bson import ObjectId
from models import User, Workout, BusyEvent, GoogleSyncRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request as GoogleRequest
from datetime import datetime, timedelta
import httpx


load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME")

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not MONGO_URI:
        raise RuntimeError("Set MONGO_URI in environment")
    if not DB_NAME:
        raise RuntimeError("Set DB_NAME in environment")
    client = AsyncIOMotorClient(MONGO_URI)
    app.state.mongodb_client = client
    app.state.mongodb = client[DB_NAME]
    await app.state.mongodb["users"].create_index("email", unique=True)
    await app.state.mongodb["users"].create_index("name", unique=True)
    try:
        yield
    finally:
        client.close()

app = FastAPI(title="BladeAPI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # or ["http://localhost:5173"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

#---------------------------------------------------------------------------
#       API Endpoints
#---------------------------------------------------------------------------

def fix_id(doc):
    """
    Helper function to convert MongoDB ObjectId to string for JSON
    serialization.
    """
    doc["_id"] = str(doc["_id"])
    return doc


@app.get("/")
async def root():
    return {"message": "Welcome to BladeAPI"}


@app.post("/add_user", status_code=201)
async def add_user(user: User, request: Request, response: Response):
    """
    Create a new user if neither email nor name exists; otherwise update existing user.

    Returns 201 on create, 200 on update.
    """
    users_col = request.app.state.mongodb["users"]
    user_dict = user.model_dump()

    # Find existing by email OR name
    existing = await users_col.find_one({"$or": [{"email": user.email}, {"name": user.name}]})

    if existing:
        # Update existing document with incoming (ignore None values to avoid overwriting with nulls)
        update_fields = {k: v for k, v in user_dict.items() if v is not None}
        if update_fields:
            await users_col.update_one({"_id": existing["_id"]}, {"$set": update_fields})
            existing.update(update_fields)
        existing["_id"] = str(existing["_id"])
        response.status_code = 200
        return {"updated": True, **existing}

    # Create new user
    try:
        res = await users_col.insert_one(user_dict)
    except Exception as e:
        if "E11000" in str(e):
            # Unique index collision (race condition) -> try update path
            existing = await users_col.find_one({"$or": [{"email": user.email}, {"name": user.name}]})
            if existing:
                update_fields = {k: v for k, v in user_dict.items() if v is not None}
                if update_fields:
                    await users_col.update_one({"_id": existing["_id"]}, {"$set": update_fields})
                    existing.update(update_fields)
                existing["_id"] = str(existing["_id"])
                response.status_code = 200
                return {"updated": True, **existing}
            raise HTTPException(status_code=409, detail="User already exists")
        raise

    user_dict["_id"] = str(res.inserted_id)
    return {"created": True, **user_dict}


@app.get("/users")
async def list_users(request: Request,
                     name: Optional[str] = Query(None),
                     email: Optional[str] = Query(None)):
    query = {}
    if name:
        query["name"] = name
    if email:
        query["email"] = email

    cursor = request.app.state.mongodb["users"].find(query)
    items = await cursor.to_list(length=100)

    return [fix_id(i) for i in items]


@app.post("/enter_workout", status_code=201)
async def enter_workout(workout: Workout, request: Request):
    workout_dict = workout.model_dump()
    
    res = await request.app.state.mongodb["workouts"].insert_one(workout_dict)
    workout_dict["_id"] = str(res.inserted_id)
    
    return workout_dict


@app.get("/workouts")
async def list_workouts(request: Request,
                        name: Optional[str] = Query(None),
                        squad: Optional[str] = Query(None),
                        username: Optional[str] = Query(None),
                        type: Optional[str] = Query(None),
                        sport: Optional[str] = Query(None)):
    query = {}
    
    if name:
        query["user.name"] = name
    if squad:
        query["squad"] = squad
    if username:
        query["user.username"] = username
    if type:
        query["type"] = type
    if sport:
        query["sport"] = sport
    
    cursor = request.app.state.mongodb["workouts"].find(query)
    items = await cursor.to_list(length=100)
    
    return [fix_id(i) for i in items]


@app.get("/workouts/{workout_id}")
async def get_workout(workout_id: str, request: Request):
    try:
        oid = ObjectId(workout_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid id")
    
    doc = await request.app.state.mongodb["workouts"].find_one({"_id": oid})
    
    if not doc:
        raise HTTPException(status_code=404, detail="not found")
    
    return fix_id(doc)


@app.delete("/workouts/{workout_id}", status_code=204)
async def delete_workout(workout_id: str, request: Request):
    try:
        oid = ObjectId(workout_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid id")
    
    res = await request.app.state.mongodb["workouts"].delete_one({"_id": oid})
    
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="not found")
    
    return None


@app.post("/add_busy_event", status_code=201)
async def add_busy_event(event: BusyEvent, request: Request):
    event_dict = event.model_dump()
    
    res = await request.app.state.mongodb["busy_events"].insert_one(event_dict)
    event_dict["_id"] = str(res.inserted_id)
    
    return event_dict


@app.get("/busy_events")
async def list_busy_events(request: Request,
                           name: Optional[str] = Query(None),
                           email: Optional[str] = Query(None),
                           date: Optional[str] = Query(None)):
    query = {}
    
    if name:
        query["name"] = name
    if email:
        query["email"] = email
    if date:
        query["date"] = date
    
    cursor = request.app.state.mongodb["busy_events"].find(query)
    items = await cursor.to_list(length=100)
    
    return [fix_id(i) for i in items]


@app.delete("/busy_events/{event_id}", status_code=204)
async def delete_busy_event(event_id: str, request: Request):
    try:
        oid = ObjectId(event_id)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid id")
    
    res = await request.app.state.mongodb["busy_events"].delete_one({"_id": oid})
    
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="not found")

    return None


@app.post("/calendar/google/sync")
async def google_sync(request: Request, payload: GoogleSyncRequest):
    users_col = request.app.state.mongodb["users"]

    # Fetch user
    user = await users_col.find_one({"email": payload.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    google = user.get("google_calendar")

    # ------------------------------
    # Case 1: user has NOT connected Google
    # ------------------------------
    if not google or "refresh_token" not in google:
        connect_url = build_google_oauth_url(str(user["_id"]))
        return {"connect_url": connect_url}

    # ------------------------------
    # Case 2: already connected → sync now
    # ------------------------------
    try:
        await sync_google_calendar_async(user, request)
    except Exception as e:
        print("Google sync failed:", e)
        raise HTTPException(status_code=500, detail="Google sync failed")

    return {"status": "synced"}

##### Helper Functions ######


async def sync_google_calendar_async(user: dict, request: Request):
    busy_col = request.app.state.mongodb["busy_events"]

    refresh_token = user["google_calendar"]["refresh_token"]
    print(refresh_token)

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/calendar.freebusy"],
    )

    # Google client is blocking → run in thread
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, creds.refresh, GoogleRequest())

    service = build("calendar", "v3", credentials=creds)

    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(days=14)).isoformat() + "Z"

    def fetch_freebusy():
        return service.freebusy().query(
            body={
                "timeMin": time_min,
                "timeMax": time_max,
                "items": [{"id": "primary"}],
            }
        ).execute()

    result = await loop.run_in_executor(None, fetch_freebusy)

    busy_blocks = result["calendars"]["primary"]["busy"]

    # Remove existing Google busy blocks
    await busy_col.delete_many({
        "email": user["email"],
        "source": "google",
    })

    # Insert new ones
    if busy_blocks:
        await busy_col.insert_many([
            {
                "name": user["name"],
                "email": user["email"],
                "start_time": b["start"],  # Changed from "start"
                "end_time": b["end"],      # Changed from "end"
                "date": b["start"][:10],   # Extract date from datetime
                "squad": user.get("squad", ""),  # Add required squad field
                "source": "google",
                "synced_at": datetime.utcnow(),
            }
            for b in busy_blocks
        ])


import urllib.parse

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

def build_google_oauth_url(user_id: str) -> str:
    params = {
        "client_id": os.environ["GOOGLE_CLIENT_ID"],
        "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"],
        "response_type": "code",
        "scope": " ".join([
            "https://www.googleapis.com/auth/calendar.freebusy",
        ]),
        "access_type": "offline",     # 🔑 refresh token
        "prompt": "consent",          # 🔑 force refresh token
        "state": user_id,             # 🔐 who is connecting
        "include_granted_scopes": "true",
    }

    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


@app.get("/auth/google/callback")
async def google_oauth_callback(
    code: str,
    state: str,
    request: Request,
):
    users_col = request.app.state.mongodb["users"]

    # Exchange code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": os.environ["GOOGLE_REDIRECT_URI"],
            },
        )
        resp.raise_for_status()
        tokens = resp.json()
        print(tokens)

    # Save refresh token 🔑
    await users_col.update_one(
        {"_id": ObjectId(state)},
        {
            "$set": {
                "google_calendar": {
                    "refresh_token": tokens["refresh_token"],
                    "access_token": tokens["access_token"],
                    "expires_at": datetime.utcnow()
                    + timedelta(seconds=tokens["expires_in"]),
                }
            }
        },
    )

    # Redirect back to frontend
    return RedirectResponse(
        url="http://localhost:5173/crew"
    )
