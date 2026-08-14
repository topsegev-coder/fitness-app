"""
main.py
================
FastAPI application entrypoint with Authentication.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection
import bcrypt
from jose import JWTError, jwt
from pathlib import Path

from database import get_db, init_db, seed_demo_data
from progression_strategies import (
    Difficulty,
    EquipmentType,
    ExerciseConfig,
    Prescription,
    ProgressionEngine,
    SessionResult,
    get_strategy,
)

# --- Security Configuration ---
SECRET_KEY = "topazi-super-secret-key-fitness" 
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def get_current_user(token: str = Depends(oauth2_scheme), conn: Connection = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    
    user = conn.execute(text("SELECT id, username FROM users WHERE username = :u"), {"u": username}).mappings().first()
    if user is None:
        raise credentials_exception
    return dict(user)

# --- App Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  
    with engine_connect() as conn:
        seed_demo_data(conn) 
    yield

def engine_connect():
    from database import engine as _engine
    return _engine.connect()

app = FastAPI(title="Smart Fitness Tracker API", lifespan=lifespan)

# --- Authentication Routes ---
class UserRegister(BaseModel):
    username: str
    email: str
    password: str

@app.post("/api/register")
def register_user(user: UserRegister, conn: Connection = Depends(get_db)):
    with conn.begin():
        existing = conn.execute(text("SELECT id FROM users WHERE username = :u OR email = :e"), {"u": user.username, "e": user.email}).first()
        if existing:
            raise HTTPException(status_code=400, detail="שם המשתמש או האימייל כבר קיימים במערכת.")
        
        hashed_password = get_password_hash(user.password)
        conn.execute(
            text("INSERT INTO users (username, email, password_hash) VALUES (:u, :e, :p)"),
            {"u": user.username, "e": user.email, "p": hashed_password}
        )
    return {"message": "המשתמש נוצר בהצלחה!"}

@app.post("/api/login")
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), conn: Connection = Depends(get_db)):
    user = conn.execute(text("SELECT * FROM users WHERE username = :u"), {"u": form_data.username}).mappings().first()
    if not user or not verify_password(form_data.password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="שם משתמש או סיסמה שגויים")
    
    expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    access_token = jwt.encode({"sub": user["username"], "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)
    return {"access_token": access_token, "token_type": "bearer", "username": user["username"]}


# --- Models ---
class ExercisePrescriptionOut(BaseModel):
    routine_exercise_id: int
    exercise_id: int
    exercise_name: str
    equipment_type: str
    weight: float
    reps_target: int
    sets: int
    consecutive_easy_count: int
    basis: str 
    target_type: Optional[str] = "reps"

class NextWorkoutOut(BaseModel):
    routine_id: int
    routine_name: str
    exercises: list[ExercisePrescriptionOut]

class CustomExerciseAdd(BaseModel):
    exercise_name: str
    equipment_type: str
    increment_step: float
    prescribed_weight: float
    prescribed_reps_target: int
    prescribed_sets: int
    target_type: Optional[str] = "reps"

class RoutineCreate(BaseModel):
    name: str
    description: Optional[str] = None
    exercises: list[CustomExerciseAdd] = []

class RoutineUpdate(BaseModel):
    name: str
    description: Optional[str] = None
    exercises: list[CustomExerciseAdd]

class RoutineDeleteBatch(BaseModel):
    routine_ids: list[int]

# --- Helper Functions ---
def _fetch_routine(conn: Connection, routine_id: int, user_id: int) -> dict:
    row = conn.execute(
        text("SELECT id, name FROM routines WHERE id = :id AND user_id = :uid"),
        {"id": routine_id, "uid": user_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Routine not found or access denied")
    return dict(row)

def _fetch_routine_exercises(conn: Connection, routine_id: int) -> list[dict]:
    rows = conn.execute(
        text("""
            SELECT re.id AS routine_exercise_id, re.exercise_id, re.prescribed_weight, re.prescribed_reps_target, re.prescribed_sets, re.consecutive_easy_count, re.target_type, e.name AS exercise_name, e.equipment_type, e.increment_step, e.min_reps_target, e.max_reps_target, e.max_weight_limit
            FROM routine_exercises re
            JOIN exercises e ON e.id = re.exercise_id
            WHERE re.routine_id = :routine_id
            ORDER BY re.display_order
        """),
        {"routine_id": routine_id},
    ).mappings().all()
    return [dict(r) for r in rows]

def _fetch_last_session_result(conn: Connection, routine_exercise_id: int, reps_target: int) -> Optional[SessionResult]:
    latest_session = conn.execute(
        text("""
            SELECT ws.id AS session_id, ws.started_at
            FROM workout_logs wl
            JOIN workout_sessions ws ON ws.id = wl.session_id
            WHERE wl.routine_exercise_id = :reid
            ORDER BY ws.started_at DESC LIMIT 1
        """),
        {"reid": routine_exercise_id},
    ).mappings().first()

    if latest_session is None: return None

    set_logs = conn.execute(
        text("SELECT reps_performed, rpe_score FROM workout_logs WHERE session_id = :session_id AND routine_exercise_id = :reid"),
        {"session_id": latest_session["session_id"], "reid": routine_exercise_id},
    ).mappings().all()

    rpe_values = [row["rpe_score"] for row in set_logs if row["rpe_score"] is not None]
    avg_rpe = sum(rpe_values) / len(rpe_values) if rpe_values else 10.0
    hit_rep_target = all(row["reps_performed"] >= reps_target for row in set_logs)
    session_date = datetime.strptime(latest_session["started_at"].replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").date()

    return SessionResult(session_date=session_date, difficulty=Difficulty.from_rpe(avg_rpe), hit_rep_target=hit_rep_target)

def _describe_basis(before: Prescription, after: Prescription, had_session: bool) -> str:
    if not had_session: return "No prior session logged — holding at current prescription."
    if after.weight > before.weight: return "Deload or weight increase applied — see weight change."
    if after.reps_target > before.reps_target: return "Progressive overload: rep target increased."
    if after.consecutive_easy_count == 0 and before.consecutive_easy_count > 0: return "Streak reset (last session wasn't rated easy)."
    if after.consecutive_easy_count > before.consecutive_easy_count: return "Easy session logged — one more to trigger progression."
    return "Holding at current prescription."

# --- UI Routes ---
@app.get("/", response_class=HTMLResponse)
def read_root():
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text(encoding="utf-8")

# --- API Routes ---
@app.get("/routine/{routine_id}/next_workout", response_model=NextWorkoutOut)
def get_next_workout(routine_id: int, current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)) -> NextWorkoutOut:
    routine = _fetch_routine(conn, routine_id, current_user["id"])
    routine_exercises = _fetch_routine_exercises(conn, routine_id)
    if not routine_exercises: raise HTTPException(status_code=404, detail="No exercises configured")

    results: list[ExercisePrescriptionOut] = []
    for re_row in routine_exercises:
        config = ExerciseConfig(
            exercise_id=re_row["exercise_id"], equipment_type=EquipmentType(re_row["equipment_type"]),
            increment_step=re_row["increment_step"], min_reps_target=re_row["min_reps_target"],
            max_reps_target=re_row["max_reps_target"], max_weight_limit=re_row["max_weight_limit"]
        )
        current = Prescription(weight=re_row["prescribed_weight"], reps_target=re_row["prescribed_reps_target"], sets=re_row["prescribed_sets"], consecutive_easy_count=re_row["consecutive_easy_count"])
        last_session = _fetch_last_session_result(conn, re_row["routine_exercise_id"], current.reps_target)
        engine = ProgressionEngine(get_strategy(config.equipment_type))
        next_prescription = engine.compute_next_prescription(config, current, last_session)

        results.append(ExercisePrescriptionOut(
            routine_exercise_id=re_row["routine_exercise_id"], exercise_id=re_row["exercise_id"], exercise_name=re_row["exercise_name"],
            equipment_type=re_row["equipment_type"], weight=next_prescription.weight, reps_target=next_prescription.reps_target,
            sets=next_prescription.sets, consecutive_easy_count=next_prescription.consecutive_easy_count,
            basis=_describe_basis(current, next_prescription, had_session=last_session is not None),
            target_type=re_row.get("target_type") or "reps"
        ))
    return NextWorkoutOut(routine_id=routine["id"], routine_name=routine["name"], exercises=results)

@app.get("/api/routines")
def get_all_routines(current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    rows = conn.execute(text("SELECT id, name, description FROM routines WHERE user_id = :uid"), {"uid": current_user["id"]}).mappings().all()
    return [dict(r) for r in rows]

@app.get("/api/next_in_rotation")
def get_next_in_rotation(current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    routines = conn.execute(text("SELECT id, name FROM routines WHERE user_id = :uid ORDER BY id"), {"uid": current_user["id"]}).mappings().all()
    if not routines: raise HTTPException(status_code=404, detail="No routines found")
    
    last_session = conn.execute(
        text("SELECT routine_id FROM workout_sessions WHERE user_id = :uid AND routine_id IS NOT NULL ORDER BY started_at DESC LIMIT 1"),
        {"uid": current_user["id"]}
    ).mappings().first()
    
    if not last_session or not last_session["routine_id"]:
        return {"routine_id": routines[0]["id"], "routine_name": routines[0]["name"]}
    
    last_id = last_session["routine_id"]
    ids = [r["id"] for r in routines]
    name_map = {r["id"]: r["name"] for r in routines}
    
    if last_id in ids:
        next_idx = (ids.index(last_id) + 1) % len(ids)
        return {"routine_id": ids[next_idx], "routine_name": name_map[ids[next_idx]]}
    return {"routine_id": ids[0], "routine_name": name_map[ids[0]]}

@app.get("/api/history")
def get_workout_history(current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    sessions = conn.execute(
        text("SELECT ws.id AS session_id, ws.started_at, ws.notes, r.name AS routine_name FROM workout_sessions ws LEFT JOIN routines r ON r.id = ws.routine_id WHERE ws.user_id = :uid ORDER BY ws.started_at DESC"),
        {"uid": current_user["id"]}
    ).mappings().all()

    history = []
    for s in sessions:
        logs = conn.execute(
            text("""
                SELECT e.name AS exercise_name, AVG(wl.weight_used) AS weight_used, AVG(wl.reps_performed) AS reps_performed, AVG(wl.rpe_score) AS rpe_score, re.target_type
                FROM workout_logs wl JOIN routine_exercises re ON re.id = wl.routine_exercise_id JOIN exercises e ON e.id = re.exercise_id
                WHERE wl.session_id = :sid GROUP BY e.id, e.name, re.target_type
            """), {"sid": s["session_id"]}
        ).mappings().all()

        formatted_logs = [{"exercise_name": l["exercise_name"], "weight_used": round(l["weight_used"], 1), "reps_performed": round(l["reps_performed"]), "rpe_score": round(l["rpe_score"], 1), "target_type": l["target_type"] or "reps"} for l in logs]
        history.append({"session_id": s["session_id"], "date": s["started_at"], "routine_name": s["routine_name"] or "אימון מותאם אישית", "notes": s["notes"], "logs": formatted_logs})
    return history

@app.post("/api/routines")
def create_routine(routine: RoutineCreate, current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    with conn.begin():
        existing = conn.execute(text("SELECT id FROM routines WHERE user_id = :uid AND LOWER(name) = LOWER(:name)"), {"uid": current_user["id"], "name": routine.name.strip()}).first()
        if existing: raise HTTPException(status_code=400, detail="כבר קיימת תוכנית אימון בשם הזה!")
        
        conn.execute(text("INSERT INTO routines (user_id, name, description) VALUES (:uid, :name, :desc)"), {"uid": current_user["id"], "name": routine.name.strip(), "desc": routine.description})
        routine_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()

        for idx, ex in enumerate(routine.exercises, start=1):
            exercise_row = conn.execute(text("SELECT id FROM exercises WHERE LOWER(name) = LOWER(:name)"), {"name": ex.exercise_name}).mappings().first()
            if exercise_row:
                exercise_id = exercise_row["id"]
            else:
                conn.execute(text("INSERT INTO exercises (name, equipment_type, increment_step, min_reps_target, max_reps_target, default_sets) VALUES (:name, :equip, :step, 8, 12, :sets)"), {"name": ex.exercise_name, "equip": ex.equipment_type, "step": ex.increment_step, "sets": ex.prescribed_sets})
                exercise_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()

            conn.execute(
                text("INSERT INTO routine_exercises (routine_id, exercise_id, display_order, prescribed_weight, prescribed_reps_target, prescribed_sets, target_type) VALUES (:rid, :eid, :order, :weight, :reps, :sets, :ttype)"),
                {"rid": routine_id, "eid": exercise_id, "order": idx, "weight": ex.prescribed_weight, "reps": ex.prescribed_reps_target, "sets": ex.prescribed_sets, "ttype": ex.target_type}
            )

    return {"id": routine_id, "message": "Routine created"}

@app.post("/api/routines/delete_batch")
def delete_routines_batch(payload: RoutineDeleteBatch, current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    with conn.begin():
        for rid in payload.routine_ids:
            conn.execute(text("DELETE FROM routines WHERE id = :rid AND user_id = :uid"), {"rid": rid, "uid": current_user["id"]})
    return {"message": "Selected routines deleted"}

class SetLog(BaseModel): set_number: int; reps_performed: int; weight_used: float; rpe_score: float
class ExerciseLog(BaseModel): routine_exercise_id: int; logs: list[SetLog]
class WorkoutSubmit(BaseModel): notes: Optional[str] = None; exercises: list[ExerciseLog]

@app.post("/routine/{routine_id}/complete")
def complete_workout(routine_id: int, workout: WorkoutSubmit, current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    with conn.begin():
        _fetch_routine(conn, routine_id, current_user["id"])
        conn.execute(text("INSERT INTO workout_sessions (user_id, routine_id, notes) VALUES (:uid, :rid, :notes)"), {"uid": current_user["id"], "rid": routine_id, "notes": workout.notes})
        session_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
        
        for ex in workout.exercises:
            for s in ex.logs:
                conn.execute(text("INSERT INTO workout_logs (session_id, routine_exercise_id, set_number, reps_performed, weight_used, rpe_score) VALUES (:sid, :reid, :snum, :reps, :weight, :rpe)"), {"sid": session_id, "reid": ex.routine_exercise_id, "snum": s.set_number, "reps": s.reps_performed, "weight": s.weight_used, "rpe": s.rpe_score})
            re_row = conn.execute(text("SELECT re.*, e.* FROM routine_exercises re JOIN exercises e ON e.id = re.exercise_id WHERE re.id = :reid"), {"reid": ex.routine_exercise_id}).mappings().first()
            if re_row:
                config = ExerciseConfig(exercise_id=re_row["id"], equipment_type=EquipmentType(re_row["equipment_type"]), increment_step=re_row["increment_step"], min_reps_target=re_row["min_reps_target"], max_reps_target=re_row["max_reps_target"], max_weight_limit=re_row["max_weight_limit"])
                current = Prescription(weight=re_row["prescribed_weight"], reps_target=re_row["prescribed_reps_target"], sets=re_row["prescribed_sets"], consecutive_easy_count=re_row["consecutive_easy_count"])
                avg_rpe = sum([s.rpe_score for s in ex.logs]) / len(ex.logs) if ex.logs else 10.0
                last_session = SessionResult(session_date=date.today(), difficulty=Difficulty.from_rpe(avg_rpe), hit_rep_target=all(s.reps_performed >= current.reps_target for s in ex.logs))
                next_presc = ProgressionEngine(get_strategy(config.equipment_type)).compute_next_prescription(config, current, last_session)
                conn.execute(text("UPDATE routine_exercises SET prescribed_weight = :weight, prescribed_reps_target = :reps, prescribed_sets = :sets, consecutive_easy_count = :streak WHERE id = :reid"), {"weight": next_presc.weight, "reps": next_presc.reps_target, "sets": next_presc.sets, "streak": next_presc.consecutive_easy_count, "reid": ex.routine_exercise_id})
    return {"message": "Workout saved", "session_id": session_id}

@app.get("/api/routines/{routine_id}/details")
def get_routine_details(routine_id: int, current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    return {"routine": _fetch_routine(conn, routine_id, current_user["id"]), "exercises": _fetch_routine_exercises(conn, routine_id)}

@app.put("/api/routines/{routine_id}")
def update_routine(routine_id: int, payload: RoutineUpdate, current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    with conn.begin():
        existing = conn.execute(text("SELECT id FROM routines WHERE user_id = :uid AND LOWER(name) = LOWER(:name) AND id != :rid"), {"uid": current_user["id"], "name": payload.name.strip(), "rid": routine_id}).first()
        if existing: raise HTTPException(status_code=400, detail="כבר קיימת תוכנית אימון בשם הזה!")
        conn.execute(text("UPDATE routines SET name = :name, description = :desc WHERE id = :rid"), {"name": payload.name.strip(), "desc": payload.description, "rid": routine_id})
        conn.execute(text("DELETE FROM routine_exercises WHERE routine_id = :rid"), {"rid": routine_id})
        for idx, ex in enumerate(payload.exercises, start=1):
            exercise_row = conn.execute(text("SELECT id FROM exercises WHERE LOWER(name) = LOWER(:name)"), {"name": ex.exercise_name}).mappings().first()
            if exercise_row:
                exercise_id = exercise_row["id"]
            else:
                conn.execute(text("INSERT INTO exercises (name, equipment_type, increment_step, min_reps_target, max_reps_target, default_sets) VALUES (:name, :equip, :step, 8, 12, :sets)"), {"name": ex.exercise_name, "equip": ex.equipment_type, "step": ex.increment_step, "sets": ex.prescribed_sets})
                exercise_id = conn.execute(text("SELECT last_insert_rowid()")).scalar()
            conn.execute(text("INSERT INTO routine_exercises (routine_id, exercise_id, display_order, prescribed_weight, prescribed_reps_target, prescribed_sets, target_type) VALUES (:rid, :eid, :order, :weight, :reps, :sets, :ttype)"), {"rid": routine_id, "eid": exercise_id, "order": idx, "weight": ex.prescribed_weight, "reps": ex.prescribed_reps_target, "sets": ex.prescribed_sets, "ttype": ex.target_type})
    return {"message": "Routine updated"}

@app.get("/api/weekly_stats")
def get_weekly_stats(current_user: dict = Depends(get_current_user), conn: Connection = Depends(get_db)):
    days_since_sunday = (datetime.now().weekday() + 1) % 7
    sunday_str = (datetime.now() - timedelta(days=days_since_sunday)).strftime("%Y-%m-%d 00:00:00")
    result = conn.execute(text("SELECT COUNT(*) AS count FROM workout_sessions WHERE user_id = :uid AND started_at >= :sunday"), {"uid": current_user["id"], "sunday": sunday_str}).mappings().first()
    return {"weekly_count": result["count"] if result else 0}