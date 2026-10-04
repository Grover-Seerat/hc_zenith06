from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from .database import get_db
from .models import Hive

router = APIRouter(prefix="/api/hives", tags=["Hives"])


@router.get("/")
def get_hives(db: Session = Depends(get_db)):
    hives = db.query(Hive).order_by(Hive.id).all()
    return hives


@router.get("/search")
def search_hives(q: str, db: Session = Depends(get_db)):
    hives = (
        db.query(Hive)
        .filter(Hive.hive_code.ilike(f"%{q}%"))
        .order_by(Hive.id)
        .all()
    )
    return hives


@router.get("/{hive_id}")
def get_hive(hive_id: int, db: Session = Depends(get_db)):
    hive = db.query(Hive).filter(Hive.id == hive_id).first()

    if not hive:
        raise HTTPException(
            status_code=404,
            detail="Hive not found"
        )

    return hive


@router.post("/")
def create_hive(
    hive_code: str,
    location: str,
    status: str = "active",
    species: str = "Apis mellifera",
    colony_strength: str = "Healthy",
    db: Session = Depends(get_db)
):
    existing = (
        db.query(Hive)
        .filter(Hive.hive_code == hive_code)
        .first()
    )

    if existing:
        raise HTTPException(
            status_code=400,
            detail="Hive code already exists"
        )

    hive = Hive(
        hive_code=hive_code,
        location=location,
        status=status,
        species=species,
        colony_strength=colony_strength
    )

    db.add(hive)
    db.commit()
    db.refresh(hive)

    return hive