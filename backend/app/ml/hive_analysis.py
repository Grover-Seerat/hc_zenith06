"""HoneyChain Hive Analysis Model.

A transparent feature-based prototype model. It scores actual sensor evidence
and the latest saved bee-health screening; it does not diagnose disease.
"""
from statistics import mean


def _fit(value, ideal, tolerance):
    if value is None:
        return None
    return max(0.0, 1.0 - abs(float(value) - ideal) / tolerance)


def analyze_hive(hive, readings, latest_ai=None):
    """Calculate a hive-condition score from supplied evidence only.

    No score is fabricated from seeded/default hive values when there is no
    actual sensor or visual evidence. Sensor readings may contain weight when
    available; otherwise weight is shown as contextual information.
    """
    readings = readings or []
    sensor_score = None
    sensor_details = {"samples": len(readings)}

    if readings:
        temps = [float(r["temperature"]) for r in readings if r.get("temperature") is not None]
        hums = [float(r["humidity"]) for r in readings if r.get("humidity") is not None]
        weights = [float(r["weight"]) for r in readings if r.get("weight") is not None]

        components = []
        if temps:
            avg_t = mean(temps)
            components.append((0.40, _fit(avg_t, 33.0, 10.0)))
            temp_dev = mean(abs(x - avg_t) for x in temps) if len(temps) > 1 else 0.0
            sensor_details["averageTemperature"] = round(avg_t, 2)
            sensor_details["temperatureStability"] = round(max(0.0, 100.0 - temp_dev * 10.0), 1)
        if hums:
            avg_h = mean(hums)
            components.append((0.30, _fit(avg_h, 60.0, 45.0)))
            hum_dev = mean(abs(x - avg_h) for x in hums) if len(hums) > 1 else 0.0
            sensor_details["averageHumidity"] = round(avg_h, 2)
            sensor_details["humidityStability"] = round(max(0.0, 100.0 - hum_dev * 2.0), 1)
        if weights:
            avg_w = mean(weights)
            components.append((0.20, _fit(avg_w, 45.0, 25.0)))
            sensor_details["averageWeight"] = round(avg_w, 2)
        if components:
            total_weight = sum(w for w, _ in components)
            stability_parts = [v for _, v in components]
            base = sum(w * v for w, v in components) / total_weight
            if len(stability_parts) > 1:
                stability = sum(stability_parts) / len(stability_parts)
            else:
                stability = stability_parts[0]
            sensor_score = round((0.80 * base + 0.20 * stability) * 100.0, 1)
            sensor_details["stabilityScore"] = round(stability * 100.0, 1)

        latest = readings[-1]
        temperature = latest.get("temperature")
        humidity = latest.get("humidity")
        weight = latest.get("weight", hive.get("weight"))
        sensor_details["samples"] = len(readings)
    else:
        temperature = None
        humidity = None
        weight = hive.get("weight") if hive else None

    visual_score = None
    if latest_ai:
        prediction = str(latest_ai.get("prediction") or "").lower()
        if latest_ai.get("visualScore") is not None:
            visual_score = round(max(0.0, min(100.0, float(latest_ai["visualScore"]))), 1)
        else:
            conf = float(latest_ai.get("confidence") or 0)
            # Legacy ai_predictions confidence stores the visual infestation score.
            visual_score = round(max(0.0, min(100.0, (1.0 - conf) * 100.0)), 1)
        if "high" in prediction:
            visual_score = min(visual_score, 35.0)
        elif "monitor" in prediction:
            visual_score = min(visual_score, 65.0)

    evidence_scores = []
    reasons = []
    if sensor_score is not None:
        evidence_scores.append((0.65, sensor_score))
        reasons.extend(["Temperature and humidity sensor evidence", "Recent sensor stability"])
        if sensor_details.get("averageWeight") is not None:
            reasons.append("Hive-weight evidence")
    if visual_score is not None:
        evidence_scores.append((0.35 if sensor_score is not None else 1.0, visual_score))
        reasons.append("Latest saved bee-health and Varroa screening")

    if evidence_scores:
        total = sum(w for w, _ in evidence_scores)
        final = round(sum(w * s for w, s in evidence_scores) / total, 1)
        status = "Healthy" if final >= 75 else "Monitor" if final >= 60 else "Needs Attention"
    else:
        final = None
        status = "Awaiting analysis"
        reasons = ["Run Bee Health with a bee image and sensor data to generate evidence."]

    return {
        "score": final,
        "status": status,
        "reasons": reasons,
        "temperature": temperature,
        "humidity": humidity,
        "weight": weight,
        "sensorScore": sensor_score,
        "sensorModel": sensor_details,
        "visualScreening": latest_ai.get("prediction") if latest_ai else None,
        "visualScore": visual_score,
        "iotTimestamp": readings[-1].get("timestamp") if readings else None,
        "model": "HoneyChain Hive Analysis Model v1.1"
    }
