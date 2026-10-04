"""Lazy-loaded honey production model.

The trained RandomForest and pandas are imported only when production
prediction is requested. This keeps normal API startup lightweight.
"""
from pathlib import Path
from datetime import date
import gc

MODEL_DIR = Path(__file__).resolve().parent / "models"
MODEL_PATH = MODEL_DIR / "honey_production_model.pkl"
FEATURES_PATH = MODEL_DIR / "features.pkl"

_FEATURES = None


def _features():
    global _FEATURES
    if _FEATURES is None:
        import pickle
        with open(FEATURES_PATH, "rb") as f:
            _FEATURES = pickle.load(f)
    return _FEATURES


def _load_model():
    import joblib
    model = joblib.load(MODEL_PATH)
    return model


def predict_honey_production(
    environmental_temperature,
    relative_humidity,
    hive_temperature,
    hive_humidity,
    wind_speed,
    total_hive_weight,
    prediction_date=None,
):
    import pandas as pd

    d = prediction_date or date.today()
    row = {
        "Environmental Temperature (°C)": float(environmental_temperature),
        "Relative Humidity (%)": float(relative_humidity),
        "Hive Temperature (°C)": float(hive_temperature),
        "Hive Humidity (%)": float(hive_humidity),
        "Wind Speed (km/h)": float(wind_speed),
        "Total Weight (Hive + Bees + Honey) (kg)": float(total_hive_weight),
        "month": int(d.month),
        "day_of_year": int(d.timetuple().tm_yday),
    }

    features = _features()
    frame = pd.DataFrame([row], columns=features)

    model = _load_model()
    try:
        prediction = float(model.predict(frame)[0])
        return max(0.0, prediction)
    finally:
        del model
        gc.collect()


def model_info():
    return {
        "model": "RandomForestRegressor",
        "features": list(_features()),
        "modelPath": str(MODEL_PATH.name),
    }
