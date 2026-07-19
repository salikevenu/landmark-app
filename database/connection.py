import os
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = None

if DATABASE_URL:
    engine = create_engine(
DATABASE_URL,
pool_pre_ping=True
)

def get_db_connection():
    if engine is None:
        raise Exception("DATABASE_URL not configured")
    return engine.connect()
