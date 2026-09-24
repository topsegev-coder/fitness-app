import os
from pathlib import Path
from typing import Generator
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

BASE_DIR = Path(__file__).resolve().parent

DEFAULT_URL = "postgresql://neondb_owner:npg_uE6FehrV1GMo@ep-fancy-hall-b4m738n5-pooler.c-6.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
DATABASE_URL = os.getenv("DATABASE_URL", DEFAULT_URL)

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine: Engine = create_engine(DATABASE_URL)

def init_db(force: bool = False) -> bool:
    return True

def get_db() -> Generator[Connection, None, None]:
    with engine.connect() as conn:
        yield conn

def seed_demo_data(conn: Connection) -> None:
    pass