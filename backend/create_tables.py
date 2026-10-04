from app.database import Base, engine
from app.models import (
    User,
    Hive,
    HoneyBatch,
    SensorReading,
    TraceabilityEvent
)

print("Creating HoneyChain database...")

Base.metadata.create_all(bind=engine)

print("Database created successfully!")