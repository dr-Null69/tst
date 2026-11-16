import os
import psycopg
from dotenv import load_dotenv

load_dotenv()

def seed():
    conn = psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "rental_variant12_test"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "12345"),
        autocommit=True
    )
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO equipment (id, code, daily_rate, stock)
            OVERRIDING SYSTEM VALUE
            VALUES (1, 'BENCHMARK-TOOL', 500, 10000000)
            ON CONFLICT (id) DO UPDATE
            SET stock = 10000000, daily_rate = 500;
        """)
        print("Test equipment (ID=1, stock=10000000) created/updated successfully.")
    conn.close()

if __name__ == "__main__":
    seed()