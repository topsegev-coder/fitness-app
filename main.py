"""
main.py
================
FastAPI application entrypoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection

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

from fastapi.responses import HTMLResponse
from pathlib import Path

from datetime import datetime, timedelta

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

def _fetch_routine(conn: Connection, routine_id: int) -> dict:
    row = conn.execute(
        text("SELECT id, name FROM routines WHERE id = :id"),
        {"id": routine_id},
    ).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} not found")
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

    if latest_session is None:
        return None

    set_logs = conn.execute(
        text("SELECT reps_performed, rpe_score FROM workout_logs WHERE session_id = :session_id AND routine_exercise_id = :reid"),
        {"session_id": latest_session["session_id"], "reid": routine_exercise_id},
    ).mappings().all()

    rpe_values = [row["rpe_score"] for row in set_logs if row["rpe_score"] is not None]
    avg_rpe = sum(rpe_values) / len(rpe_values) if rpe_values else 10.0
    hit_rep_target = all(row["reps_performed"] >= reps_target for row in set_logs)

    session_date = datetime.strptime(latest_session["started_at"], "%Y-%m-%d %H:%M:%S").date()

    return SessionResult(
        session_date=session_date,
        difficulty=Difficulty.from_rpe(avg_rpe),
        hit_rep_target=hit_rep_target,
    )

def _describe_basis(before: Prescription, after: Prescription, had_session: bool) -> str:
    if not had_session: return "No prior session logged — holding at current prescription."
    if after.weight > before.weight: return "Deload or weight increase applied — see weight change."
    if after.reps_target > before.reps_target: return "Progressive overload: rep target increased."
    if after.consecutive_easy_count == 0 and before.consecutive_easy_count > 0: return "Streak reset (last session wasn't rated easy)."
    if after.consecutive_easy_count > before.consecutive_easy_count: return "Easy session logged — one more to trigger progression."
    return "Holding at current prescription."

@app.get("/routine/{routine_id}/next_workout", response_model=NextWorkoutOut)
def get_next_workout(routine_id: int, conn: Connection = Depends(get_db)) -> NextWorkoutOut:
    routine = _fetch_routine(conn, routine_id)
    routine_exercises = _fetch_routine_exercises(conn, routine_id)

    if not routine_exercises:
        raise HTTPException(status_code=404, detail=f"Routine {routine_id} has no exercises configured")

    results: list[ExercisePrescriptionOut] = []

    for re_row in routine_exercises:
        config = ExerciseConfig(
            exercise_id=re_row["exercise_id"],
            equipment_type=EquipmentType(re_row["equipment_type"]),
            increment_step=re_row["increment_step"],
            min_reps_target=re_row["min_reps_target"],
            max_reps_target=re_row["max_reps_target"],
            max_weight_limit=re_row["max_weight_limit"],
        )

        current = Prescription(
            weight=re_row["prescribed_weight"],
            reps_target=re_row["prescribed_reps_target"],
            sets=re_row["prescribed_sets"],
            consecutive_easy_count=re_row["consecutive_easy_count"],
        )

        last_session = _fetch_last_session_result(conn, re_row["routine_exercise_id"], current.reps_target)

        strategy = get_strategy(config.equipment_type)
        engine = ProgressionEngine(strategy)
        next_prescription = engine.compute_next_prescription(config, current, last_session)

        results.append(
            ExercisePrescriptionOut(
                routine_exercise_id=re_row["routine_exercise_id"],
                exercise_id=re_row["exercise_id"],
                exercise_name=re_row["exercise_name"],
                equipment_type=re_row["equipment_type"],
                weight=next_prescription.weight,
                reps_target=next_prescription.reps_target,
                sets=next_prescription.sets,
                consecutive_easy_count=next_prescription.consecutive_easy_count,
                basis=_describe_basis(current, next_prescription, had_session=last_session is not None),
                target_type=re_row.get("target_type") or "reps"
            )
        )

    return NextWorkoutOut(routine_id=routine["id"], routine_name=routine["name"], exercises=results)

@app.get("/", response_class=HTMLResponse)
def read_root():
    html_path = Path(__file__).parent / "index.html"
    return html_path.read_text(encoding="utf-8")

@app.get("/health")
def health_check() -> dict:
    return {"status": "ok"}


# ----------------------------------------------------------------------------
# Workout Completion (POST Endpoint)
# ----------------------------------------------------------------------------

class SetLog(BaseModel):
    set_number: int
    reps_performed: int
    weight_used: float
    rpe_score: float

class ExerciseLog(BaseModel):
    routine_exercise_id: int
    logs: list[SetLog]

class WorkoutSubmit(BaseModel):
    notes: Optional[str] = None
    exercises: list[ExerciseLog]

# ----------------------------------------------------------------------------
# Routine & Exercise Management (CRUD)
# ----------------------------------------------------------------------------

class RoutineCreate(BaseModel):
    name: str
    description: Optional[str] = None

class CustomExerciseAdd(BaseModel):
    exercise_name: str
    equipment_type: str
    increment_step: float
    prescribed_weight: float
    prescribed_reps_target: int
    prescribed_sets: int
    target_type: Optional[str] = "reps"

class RoutineDeleteBatch(BaseModel):
    routine_ids: list[int]

@app.get("/api/routines")
def get_all_routines(conn: Connection = Depends(get_db)):
    rows = conn.execute(text("SELECT id, name, description FROM routines")).mappings().all()
    return [dict(r) for r in rows]

@app.get("/api/exercises")
def get_all_exercises(conn: Connection = Depends(get_db)):
    rows = conn.execute(text("SELECT id, name, equipment_type FROM exercises")).mappings().all()
    return [dict(r) for r in rows]

@app.get("/api/next_in_rotation")
def get_next_in_rotation(conn: Connection = Depends(get_db)):
    routines = conn.execute(text("SELECT id, name FROM routines ORDER BY id")).mappings().all()
    if not routines:
        raise HTTPException(status_code=404, detail="No routines found")
    
    last_session = conn.execute(
        text("SELECT routine_id FROM workout_sessions WHERE routine_id IS NOT NULL ORDER BY started_at DESC LIMIT 1")
    ).mappings().first()
    
    if not last_session or not last_session["routine_id"]:
        return {"routine_id": routines[0]["id"], "routine_name": routines[0]["name"]}
    
    last_id = last_session["routine_id"]
    ids = [r["id"] for r in routines]
    name_map = {r["id"]: r["name"] for r in routines}
    
    if last_id in ids:
        next_idx = (ids.index(last_id) + 1) % len(ids)
        next_id = ids[next_idx]
        return {"routine_id": next_id, "routine_name": name_map[next_id]}
    return {"routine_id": ids[0], "routine_name": name_map[ids[0]]}

@app.get("/api/history")
def get_workout_history(conn: Connection = Depends(get_db)):
    sessions = conn.execute(
        text("""
            SELECT ws.id AS session_id, ws.started_at, ws.notes, r.name AS routine_name
            FROM workout_sessions ws
            LEFT JOIN routines r ON r.id = ws.routine_id
            ORDER BY ws.started_at DESC
        """)
    ).mappings().all()

    history = []
    for s in sessions:
        logs = conn.execute(
            text("""
                SELECT e.name AS exercise_name, 
                       AVG(wl.weight_used) AS weight_used, 
                       AVG(wl.reps_performed) AS reps_performed, 
                       AVG(wl.rpe_score) AS rpe_score,
                       re.target_type
                FROM workout_logs wl
                JOIN routine_exercises re ON re.id = wl.routine_exercise_id
                JOIN exercises e ON e.id = re.exercise_id
                WHERE wl.session_id = :sid
                GROUP BY e.id, e.name, re.target_type
            """),
            {"sid": s["session_id"]}
        ).mappings().all()

        formatted_logs = []
        for l in logs:
            formatted_logs.append({
                "exercise_name": l["exercise_name"],
                "weight_used": round(l["weight_used"], 1),
                "reps_performed": round(l["reps_performed"]),
                "rpe_score": round(l["rpe_score"], 1),
                "target_type": l["target_type"] or "reps"
            })

        history.append({
            "session_id": s["session_id"],
            "date": s["started_at"],
            "routine_name": s["routine_name"] or "אימון מותאם אישית",
            "notes": s["notes"],
            "logs": formatted_logs
        })

    return history

@app.post("/api/routines")
def create_routine(routine: RoutineCreate, conn: Connection = Depends(get_db)):
    user_id = 1
    with conn.begin():
        existing = conn.execute(
            text("SELECT id FROM routines WHERE user_id = :uid AND LOWER(name) = LOWER(:name)"),
            {"uid": user_id, "name": routine.name.strip()}
        ).first()
        
        if existing is not None:
            raise HTTPException(status_code=400, detail="כבר קיימת תוכנית אימון בשם הזה!")

        result = conn.execute(
            text("INSERT INTO routines (user_id, name, description) VALUES (:uid, :name, :desc)"),
            {"uid": user_id, "name": routine.name.strip(), "desc": routine.description}
        )
        routine_id = result.lastrowid
    return {"id": routine_id, "message": "Routine created successfully"}

@app.post("/api/routines/delete_batch")
def delete_routines_batch(payload: RoutineDeleteBatch, conn: Connection = Depends(get_db)):
    with conn.begin():
        for rid in payload.routine_ids:
            conn.execute(text("DELETE FROM routines WHERE id = :rid"), {"rid": rid})
    return {"message": "Selected routines deleted successfully"}

@app.post("/api/routine/{routine_id}/add_custom_exercise")
def add_custom_exercise_to_routine(routine_id: int, ex: CustomExerciseAdd, conn: Connection = Depends(get_db)):
    with conn.begin():
        exercise_row = conn.execute(
            text("SELECT id FROM exercises WHERE LOWER(name) = LOWER(:name)"),
            {"name": ex.exercise_name}
        ).mappings().first()

        if exercise_row:
            exercise_id = exercise_row["id"]
        else:
            res_ex = conn.execute(
                text("""
                    INSERT INTO exercises (name, equipment_type, increment_step, min_reps_target, max_reps_target, default_sets)
                    VALUES (:name, :equipment_type, :increment_step, 8, 12, :sets)
                """),
                {
                    "name": ex.exercise_name, 
                    "equipment_type": ex.equipment_type, 
                    "increment_step": ex.increment_step, 
                    "sets": ex.prescribed_sets
                }
            )
            exercise_id = res_ex.lastrowid

        order_row = conn.execute(
            text("SELECT COALESCE(MAX(display_order), 0) + 1 AS next_order FROM routine_exercises WHERE routine_id = :rid"),
            {"rid": routine_id}
        ).mappings().first()
        
        conn.execute(
            text("""
                INSERT INTO routine_exercises 
                (routine_id, exercise_id, display_order, prescribed_weight, prescribed_reps_target, prescribed_sets, target_type)
                VALUES (:rid, :eid, :order, :weight, :reps, :sets, :ttype)
            """),
            {
                "rid": routine_id, 
                "eid": exercise_id, 
                "order": order_row["next_order"],
                "weight": ex.prescribed_weight, 
                "reps": ex.prescribed_reps_target, 
                "sets": ex.prescribed_sets,
                "ttype": ex.target_type
            }
        )
    return {"message": "Custom exercise added successfully"}

@app.post("/routine/{routine_id}/complete")
def complete_workout(routine_id: int, workout: WorkoutSubmit, conn: Connection = Depends(get_db)):
    user_id = 1
    
    with conn.begin():
        result = conn.execute(
            text("INSERT INTO workout_sessions (user_id, routine_id, notes) VALUES (:uid, :rid, :notes)"),
            {"uid": user_id, "rid": routine_id, "notes": workout.notes}
        )
        session_id = result.lastrowid

        for ex in workout.exercises:
            for s in ex.logs:
                conn.execute(
                    text("""
                        INSERT INTO workout_logs (session_id, routine_exercise_id, set_number, reps_performed, weight_used, rpe_score)
                        VALUES (:sid, :reid, :snum, :reps, :weight, :rpe)
                    """),
                    {
                        "sid": session_id, 
                        "reid": ex.routine_exercise_id, 
                        "snum": s.set_number, 
                        "reps": s.reps_performed, 
                        "weight": s.weight_used, 
                        "rpe": s.rpe_score
                    }
                )
            
            re_row = conn.execute(
                text("""
                    SELECT re.prescribed_weight, re.prescribed_reps_target, re.prescribed_sets, re.consecutive_easy_count,
                           e.id as exercise_id, e.equipment_type, e.increment_step, e.min_reps_target, e.max_reps_target, e.max_weight_limit
                    FROM routine_exercises re
                    JOIN exercises e ON e.id = re.exercise_id
                    WHERE re.id = :reid
                """),
                {"reid": ex.routine_exercise_id}
            ).mappings().first()

            if re_row:
                config = ExerciseConfig(
                    exercise_id=re_row["exercise_id"],
                    equipment_type=EquipmentType(re_row["equipment_type"]),
                    increment_step=re_row["increment_step"],
                    min_reps_target=re_row["min_reps_target"],
                    max_reps_target=re_row["max_reps_target"],
                    max_weight_limit=re_row["max_weight_limit"],
                )
                
                current = Prescription(
                    weight=re_row["prescribed_weight"],
                    reps_target=re_row["prescribed_reps_target"],
                    sets=re_row["prescribed_sets"],
                    consecutive_easy_count=re_row["consecutive_easy_count"],
                )

                rpe_values = [s.rpe_score for s in ex.logs]
                avg_rpe = sum(rpe_values) / len(rpe_values) if rpe_values else 10.0
                hit_rep_target = all(s.reps_performed >= current.reps_target for s in ex.logs)
                
                last_session = SessionResult(
                    session_date=date.today(),
                    difficulty=Difficulty.from_rpe(avg_rpe),
                    hit_rep_target=hit_rep_target,
                )

                engine = ProgressionEngine(get_strategy(config.equipment_type))
                next_presc = engine.compute_next_prescription(config, current, last_session)

                conn.execute(
                    text("""
                        UPDATE routine_exercises
                        SET prescribed_weight = :weight,
                            prescribed_reps_target = :reps,
                            prescribed_sets = :sets,
                            consecutive_easy_count = :streak
                        WHERE id = :reid
                    """),
                    {
                        "weight": next_presc.weight,
                        "reps": next_presc.reps_target,
                        "sets": next_presc.sets,
                        "streak": next_presc.consecutive_easy_count,
                        "reid": ex.routine_exercise_id
                    }
                )
        
    return {"message": "Workout saved & next workout updated successfully!", "session_id": session_id}

class RoutineUpdate(BaseModel):
    name: str
    description: Optional[str] = None
    exercises: list[CustomExerciseAdd]

@app.get("/api/routines/{routine_id}/details")
def get_routine_details(routine_id: int, conn: Connection = Depends(get_db)):
    routine = _fetch_routine(conn, routine_id)
    exercises = _fetch_routine_exercises(conn, routine_id)
    return {
        "routine": routine,
        "exercises": exercises
    }

@app.put("/api/routines/{routine_id}")
def update_routine(routine_id: int, payload: RoutineUpdate, conn: Connection = Depends(get_db)):
    user_id = 1
    with conn.begin():
        # בדיקה האם השם החדש כבר תפוס על ידי אימון אחר
        existing = conn.execute(
            text("SELECT id FROM routines WHERE user_id = :uid AND LOWER(name) = LOWER(:name) AND id != :rid"),
            {"uid": user_id, "name": payload.name.strip(), "rid": routine_id}
        ).first()
        if existing is not None:
            raise HTTPException(status_code=400, detail="כבר קיימת תוכנית אימון בשם הזה!")

        # עדכון שם האימון
        conn.execute(
            text("UPDATE routines SET name = :name, description = :desc WHERE id = :rid"),
            {"name": payload.name.strip(), "desc": payload.description, "rid": routine_id}
        )
        
        # מחיקת התרגילים הישנים של האימון
        conn.execute(text("DELETE FROM routine_exercises WHERE routine_id = :rid"), {"rid": routine_id})

        # הוספת התרגילים המעודכנים מחדש
        for idx, ex in enumerate(payload.exercises, start=1):
            exercise_row = conn.execute(
                text("SELECT id FROM exercises WHERE LOWER(name) = LOWER(:name)"),
                {"name": ex.exercise_name}
            ).mappings().first()

            if exercise_row:
                exercise_id = exercise_row["id"]
            else:
                res_ex = conn.execute(
                    text("""
                        INSERT INTO exercises (name, equipment_type, increment_step, min_reps_target, max_reps_target, default_sets)
                        VALUES (:name, :equipment_type, :increment_step, 8, 12, :sets)
                    """),
                    {
                        "name": ex.exercise_name, 
                        "equipment_type": ex.equipment_type, 
                        "increment_step": ex.increment_step, 
                        "sets": ex.prescribed_sets
                    }
                )
                exercise_id = res_ex.lastrowid

            conn.execute(
                text("""
                    INSERT INTO routine_exercises 
                    (routine_id, exercise_id, display_order, prescribed_weight, prescribed_reps_target, prescribed_sets, target_type)
                    VALUES (:rid, :eid, :order, :weight, :reps, :sets, :ttype)
                """),
                {
                    "rid": routine_id, 
                    "eid": exercise_id, 
                    "order": idx,
                    "weight": ex.prescribed_weight, 
                    "reps": ex.prescribed_reps_target, 
                    "sets": ex.prescribed_sets,
                    "ttype": ex.target_type
                }
            )
    return {"message": "Routine updated successfully"}

@app.get("/api/weekly_stats")
def get_weekly_stats(conn: Connection = Depends(get_db)):
    user_id = 1
    # חישוב תאריך יום ראשון האחרון של השבוע הנוכחי
    today = datetime.now()
    # weekday(): 0=Monday, ..., 6=Sunday. נתאים כך שיום ראשון הוא תחילת השבוע
    days_since_sunday = (today.weekday() + 1) % 7
    sunday = today - timedelta(days=days_since_sunday)
    sunday_str = sunday.strftime("%Y-%m-%d 00:00:00")

    result = conn.execute(
        text("""
            SELECT COUNT(*) AS count 
            FROM workout_sessions 
            WHERE user_id = :uid AND started_at >= :sunday
        """),
        {"uid": user_id, "sunday": sunday_str}
    ).mappings().first()

    return {"weekly_count": result["count"] if result else 0}