#!/usr/bin/env python3
"""Run once to initialize the database schema."""
import os
from dotenv import load_dotenv
import psycopg2

load_dotenv()

conn = psycopg2.connect(os.environ["DATABASE_URL"])
with open("schema.sql") as f:
    sql = f.read()
with conn.cursor() as cur:
    cur.execute(sql)
conn.commit()
conn.close()
print("Schema initialized successfully.")
