from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.sql import func

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255))
    role = Column(String(50), nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )


class Hive(Base):
    __tablename__ = "hives"

    id = Column(Integer, primary_key=True, index=True)

    hive_code = Column(
        String(100),
        unique=True,
        nullable=False,
        index=True
    )

    beekeeper_id = Column(
        Integer,
        ForeignKey("users.id"),
        nullable=True
    )

    location = Column(String(255))
    latitude = Column(Float)
    longitude = Column(Float)

    status = Column(
        String(50),
        default="active"
    )

    species = Column(String(100))
    colony_strength = Column(String(100))

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )


class HoneyBatch(Base):
    __tablename__ = "honey_batches"

    id = Column(Integer, primary_key=True, index=True)

    batch_code = Column(
        String(100),
        unique=True,
        nullable=False,
        index=True
    )

    hive_id = Column(
        Integer,
        ForeignKey("hives.id")
    )

    beekeeper_id = Column(
        Integer,
        ForeignKey("users.id")
    )

    harvest_date = Column(String(50))
    honey_type = Column(String(100))

    quantity = Column(Float)
    unit = Column(String(30))

    status = Column(
        String(50),
        default="harvested"
    )

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id = Column(Integer, primary_key=True, index=True)

    hive_id = Column(
        Integer,
        ForeignKey("hives.id")
    )

    temperature = Column(Float)
    humidity = Column(Float)
    weight = Column(Float)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now()
    )


class TraceabilityEvent(Base):
    __tablename__ = "traceability_events"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("honey_batches.id"))
    event_type = Column(String(100))
    actor_id = Column(Integer, ForeignKey("users.id"))
    event_metadata = Column(Text)
    previous_hash = Column(String(128))
    event_hash = Column(String(128))
    created_at = Column(DateTime(timezone=True), server_default=func.now())