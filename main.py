import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Response, Body, Query
import asyncio
import datetime
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request as GoogleRequest
from fastapi.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional
from contextlib import asynccontextmanager
from bson import ObjectId
from models import User, Workout, BusyEvent

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
#       Google Calendar Helper Functions
#---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

def _build_service_from_oauth_sync(client_id: str,
                                   client_secret: str,
                                   refresh_token: str):
    """
    Synchronous: construct google calendar service from OAuth refresh token.
    Intended to be run in a thread via asyncio.to_thread(...) to avoid blocking.
    """
    creds = Credentials(
        None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    # refresh token to get an access token if needed
    req = GoogleRequest()
    if not creds.valid:
        creds.refresh(req)
    service = build("calendar", "v3",
                    credentials=creds,#
                    cache_discovery=False)
    return service

def _build_service_from_service_account_sync(keyfile_path: str,
                                             subject: str | None = None):
    """
    Synchronous: construct google calendar service from a service account keyfile.
    """
    creds = service_account.Credentials.from_service_account_file(keyfile_path,
                                                                  scopes=SCOPES)
    if subject:
        creds = creds.with_subject(subject)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return service

def _fetch_all_events_sync(auth_payload: dict,
                           calendar_id: str,
                           time_min: str,
                           time_max: str):
    """
    Synchronous helper that pages through Google Calendar events and returns a
    list of events.
    Run in a thread (asyncio.to_thread) from async code.
    """
    auth_type = auth_payload.get("auth_type")
    if auth_type == "oauth":
        required = ["client_id", "client_secret", "refresh_token"]
        for r in required:
            if r not in auth_payload:
                raise ValueError(f"{r} required for oauth")
        service = _build_service_from_oauth_sync(
            auth_payload["client_id"],
            auth_payload["client_secret"],
            auth_payload["refresh_token"],
        )
    elif auth_type == "service_account":
        if "service_account_keyfile" not in auth_payload:
            raise ValueError("service_account_keyfile required for service_account")
        service = _build_service_from_service_account_sync(
            auth_payload["service_account_keyfile"],
            auth_payload.get("subject"),
        )
    else:
        raise ValueError("unknown auth_type")

    events = []
    page_token = None
    events_resource = service.events()
    while True:
        resp = events_resource.list(
            calendarId=calendar_id,
            pageToken=page_token,
            singleEvents=True,
            orderBy="startTime",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=2500,
        ).execute()
        items = resp.get("items", [])
        events.extend(items)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return events

def _normalize_event(event: dict) -> dict:
    """
    Convert an event resource into a concise document for Mongo.
    Keeps original in `raw`.
    """
    return {
        "id": event.get("id"),
        "status": event.get("status"),
        "summary": event.get("summary"),
        "description": event.get("description"),
        "location": event.get("location"),
        "created": event.get("created"),
        "updated": event.get("updated"),
        "start": event.get("start"),
        "end": event.get("end"),
        "recurrence": event.get("recurrence"),
        "attendees": event.get("attendees"),
        "organizer": event.get("organizer"),
        "raw": event,
    }

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
        query["user.name"] = name
    if email:
        query["user.email"] = email
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


@app.post("/sync-calendar")
async def sync_calendar(request: Request, payload: dict = Body(...)):
    """
    POST body JSON (either OAuth or Service Account):
    OAuth example:
    {
      "auth_type": "oauth",
      "client_id": "...",
      "client_secret": "...",
      "refresh_token": "...",
      "calendarId": "primary"           # optional
    }

    Service account example:
    {
      "auth_type": "service_account",
      "service_account_keyfile": "/path/to/key.json",
      "subject": "user@example.com",    # optional, for domain-wide delegation
      "calendarId": "primary"
    }

    Optional fields:
      timeMin (RFC3339) default "1970-01-01T00:00:00Z"
      timeMax (RFC3339) default now + 10 years
    """
    # Validate and set defaults
    calendar_id = payload.get("calendarId", "primary")
    time_min = payload.get("timeMin", "1970-01-01T00:00:00Z")
    time_max = payload.get("timeMax")
    if not time_max:
        time_max = (datetime.datetime.utcnow() + datetime.timedelta(days=3650)).isoformat() + "Z"

    try:
        # Run blocking Google calls in a thread
        events = await asyncio.to_thread(_fetch_all_events_sync, payload, calendar_id, time_min, time_max)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Google API error: {e}")

    # Upsert into MongoDB (async via motor)
    coll = request.app.state.mongodb["calendar_events"]
    total = len(events)
    inserted = 0
    updated = 0
    errors = []

    for ev in events:
        doc = _normalize_event(ev)
        if not doc.get("id"):
            errors.append({"event_no_id": ev})
            continue
        try:
            res = await coll.update_one({"id": doc["id"]}, {"$set": doc}, upsert=True)
            # Motor's update_one returns upserted_id if inserted
            if getattr(res, "upserted_id", None):
                inserted += 1
            else:
                # If matched_count > 0 and no upserted_id => updated
                if getattr(res, "matched_count", 0) > 0:
                    updated += 1
        except Exception as e:
            errors.append({"id": doc.get("id"), "error": str(e)})

    return {
        "status": "ok",
        "calendarId": calendar_id,
        "total_fetched": total,
        "inserted": inserted,
        "updated": updated,
        "errors": errors,
    }
