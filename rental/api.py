from dotenv import load_dotenv
load_dotenv()
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import psycopg

from rental.database import connect
from rental.repositories import PostgresEquipmentRepository, PostgresRentalRepository
from rental.service import RentalService, RentalRejectedError
from rental.models import RentalRequest

app = FastAPI(title="Rental API - Variant 12")



def get_db_connection():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "rental_variant12_test"),
        user=os.getenv("DB_USER", "rental_student"),
        password=os.getenv("DB_PASSWORD", ""),
        autocommit=True
    )

class DummyPolicy:
    def is_allowed(self, equipment, quantity, days):
        return True

@app.on_event("startup")
def startup_event():
    app.state.conn = get_db_connection()
    app.state.equip_repo = PostgresEquipmentRepository(app.state.conn)
    app.state.rental_repo = PostgresRentalRepository(app.state.conn)
    app.state.service = RentalService(app.state.equip_repo, app.state.rental_repo, DummyPolicy())

@app.on_event("shutdown")
def shutdown_event():
    app.state.conn.close()


class CreateRentalDTO(BaseModel):
    reference: str
    equipment_id: int
    quantity: int
    days: int
    premium: bool = False

class ReturnRentalDTO(BaseModel):
    overdue_days: int = 0



def error_response(status_code: int, code: str, message: str):
    return JSONResponse(status_code=status_code, content={"code": code, "message": message})

@app.exception_handler(ValueError)
def value_error_handler(request: Request, exc: ValueError):
    return error_response(400, "BAD_REQUEST", str(exc))

@app.exception_handler(RentalRejectedError)
def rejected_error_handler(request: Request, exc: RentalRejectedError):
    return error_response(409, "CONFLICT", str(exc))


# --- Endpoints ---
@app.post("/rentals", status_code=201)
def create_rental(dto: CreateRentalDTO):
    req = RentalRequest(dto.reference, dto.equipment_id, dto.quantity, dto.days, dto.premium)

    rental = app.state.service.rent(req)

    return rental

@app.get("/rentals/{rental_id}", status_code=200)
def get_rental(rental_id: int):
    order = app.state.rental_repo.find_by_id(rental_id)
    if not order:
        return error_response(404, "NOT_FOUND", f"Оренду {rental_id} не знайдено")
    return order

@app.post("/rentals/{rental_id}/return", status_code=200)
def return_rental(rental_id: int, dto: ReturnRentalDTO):
    return app.state.service.return_rental(rental_id, dto.overdue_days)