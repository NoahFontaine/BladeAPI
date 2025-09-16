import os
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorClient
from typing import Optional
from contextlib import asynccontextmanager
from bson import ObjectId
from models import Workout

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
    try:
        yield
    finally:
        client.close()

app = FastAPI(title="BladeAPI", lifespan=lifespan)

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


@app.post("/enter_workout", status_code=201)
async def enter_workout(workout: Workout, request: Request):
    workout_dict = workout.model_dump()
    
    res = await request.app.state.mongodb["workouts"].insert_one(workout_dict)
    workout_dict["_id"] = str(res.inserted_id)
    
    return workout_dict


@app.get("/workouts")
async def list_workouts(request: Request,
                        name: Optional[str] = Query(None),
                        type: Optional[str] = Query(None),
                        sport: Optional[str] = Query(None)):
    query = {}
    
    if name:
        query["name"] = name
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
