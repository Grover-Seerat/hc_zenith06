"""Memory-conscious HoneyChain image inference.

The original implementation loaded four ML models at module import time.
That made a FastAPI/uvicorn startup consume far more RAM than a 512 MB
container can reliably provide.

This version:
- imports PyTorch/Ultralytics only when image inference is requested;
- uses TorchScript for the Bee CNN and EfficientNet models, so torchvision
  model construction is not needed at runtime for those two models;
- loads one model at a time and explicitly releases it after inference;
- limits PyTorch CPU thread pools to keep thread memory bounded.

The prediction schema and fusion weights remain unchanged.
"""
from pathlib import Path
from PIL import Image
import io
import gc
import os
import numpy as np

BASE = Path(__file__).resolve().parent
MODEL_DIR = BASE / "models"

# Keep native numerical thread pools small in low-memory containers.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

BEE_CLASSES = {
    "0": "healthy",
    "1": "few varroa / hive beetles",
    "2": "varroa / small hive beetles",
    "3": "ant problems",
    "4": "hive being robbed",
    "5": "missing queen",
}


def _configure_torch(torch):
    """Configure CPU execution for a small container."""
    try:
        torch.set_num_threads(int(os.getenv("HONEYCHAIN_TORCH_THREADS", "1")))
    except Exception:
        pass
    try:
        torch.set_num_interop_threads(1)
    except Exception:
        # PyTorch raises if inter-op threads were already configured.
        pass


def _torch():
    import torch
    _configure_torch(torch)
    return torch


def _image(x):
    if isinstance(x, Image.Image):
        return x.convert("RGB")
    return Image.open(io.BytesIO(x)).convert("RGB")


def _tensor_from_image(image, size, mean=None, std=None):
    """Create a small CHW float32 tensor without importing torchvision."""
    torch = _torch()
    arr = np.asarray(image.resize((size, size), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
    arr = np.transpose(arr, (2, 0, 1))
    if mean is not None and std is not None:
        mean_arr = np.asarray(mean, dtype=np.float32).reshape(3, 1, 1)
        std_arr = np.asarray(std, dtype=np.float32).reshape(3, 1, 1)
        arr = (arr - mean_arr) / std_arr
    return torch.from_numpy(arr).unsqueeze(0)


def _load_torchscript(path):
    torch = _torch()
    model = torch.jit.load(str(path), map_location="cpu")
    model.eval()
    return torch, model


def _predict_bee_health(image):
    """Run the small BeeCNN, then release it before loading the next model."""
    torch, model = _load_torchscript(MODEL_DIR / "bee_health_cnn_best.ts")
    try:
        x = _tensor_from_image(image, 100)
        with torch.inference_mode():
            probs = torch.softmax(model(x), dim=1)[0].cpu().tolist()
        bee_idx = int(max(range(len(probs)), key=lambda i: probs[i]))
        bee_infestation = float(probs[1] + probs[2])
        return probs, bee_idx, bee_infestation
    finally:
        del model
        gc.collect()


def _predict_efficientnet(image):
    """Run the EfficientNet model, then release it."""
    torch, model = _load_torchscript(MODEL_DIR / "efficientnet_b0_varroa_best.ts")
    try:
        x = _tensor_from_image(
            image,
            224,
            mean=[0.485, 0.456, 0.406],
            std=[0.229, 0.224, 0.225],
        )
        with torch.inference_mode():
            probs = torch.softmax(model(x), dim=1)[0].cpu().tolist()
        return probs, float(probs[1])
    finally:
        del model
        gc.collect()


def _predict_yolo(image, model_path, conf):
    """Load one Ultralytics model, infer once, then release it."""
    # Ultralytics imports torch and its dependencies only when this path is used.
    from ultralytics import YOLO

    model = YOLO(str(model_path))
    try:
        result = model.predict(source=image, conf=conf, verbose=False)[0]
        return result
    finally:
        del model
        gc.collect()


def _bee_detections(image):
    path = MODEL_DIR / "bee_final.pt"
    if not path.exists():
        return [], 0, 0, False

    result = _predict_yolo(image, path, 0.20)
    detections = []
    for b in result.boxes:
        cls_id = int(b.cls[0].cpu()) if b.cls is not None else 0
        confidence = float(b.conf[0].cpu())
        names = result.names if hasattr(result, "names") else {}
        cls_name = names.get(cls_id, str(cls_id)) if hasattr(names, "get") else str(cls_id)
        detections.append({
            "confidence": confidence,
            "class": cls_name,
        })

    bee_count = sum(1 for x in detections if x["class"] == "bee")
    blurred_count = sum(1 for x in detections if x["class"] == "blurred_bee")
    del result
    gc.collect()
    return detections, bee_count, blurred_count, True


def _varroa_detections(image):
    result = _predict_yolo(
        image,
        MODEL_DIR / "varroa_yolo11n_best.pt",
        0.25,
    )
    boxes = []
    for b in result.boxes:
        xyxy = b.xyxy[0].cpu().tolist()
        boxes.append({
            "x1": xyxy[0],
            "y1": xyxy[1],
            "x2": xyxy[2],
            "y2": xyxy[3],
            "confidence": float(b.conf[0].cpu()),
            "class": "varroa_mite",
        })
    del result
    gc.collect()
    yolo_score = max((x["confidence"] for x in boxes), default=0.0)
    return boxes, yolo_score


def predict_image(raw):
    """Run the complete visual fusion pipeline with bounded model memory."""
    image = _image(raw)

    # Models are intentionally executed sequentially. Keeping all four in RAM
    # simultaneously was the main source of container OOM during startup.
    bp, bee_idx, bee_infestation = _predict_bee_health(image)
    gc.collect()

    ep, eff_infestation = _predict_efficientnet(image)
    gc.collect()

    bee_detections, bee_count, blurred_count, detector_available = _bee_detections(image)
    gc.collect()

    boxes, yolo_score = _varroa_detections(image)
    gc.collect()

    visual_score = (
        0.35 * bee_infestation
        + 0.50 * eff_infestation
        + 0.15 * yolo_score
    )
    if visual_score >= 0.75:
        band = "High Concern"
    elif visual_score >= 0.50:
        band = "Monitor"
    else:
        band = "Low Concern"

    return {
        "visualScore": round(visual_score, 4),
        "visualStatus": band,
        "beeHealth": {
            "class": BEE_CLASSES.get(str(bee_idx), str(bee_idx)),
            "confidence": round(float(bp[bee_idx]), 4),
            "infestationEvidence": round(bee_infestation, 4),
            "probabilities": {
                BEE_CLASSES.get(str(i), str(i)): round(float(p), 4)
                for i, p in enumerate(bp)
            },
        },
        "beeDetector": {
            "available": detector_available,
            "detections": bee_detections,
            "beeCount": bee_count,
            "blurredBeeCount": blurred_count,
        },
        "varroa": {
            "infestedProbability": round(eff_infestation, 4),
            "miteCount": len(boxes),
            "maxDetectionConfidence": round(yolo_score, 4),
            "boxes": boxes,
        },
        "fusion": {
            "beeImageWeight": 0.35,
            "verrdoEfficientNetWeight": 0.50,
            "verrdoYoloWeight": 0.15,
            "thresholds": {"monitor": 0.50, "high": 0.75},
        },
        "modelVersion": "HoneyChain visual fusion v1.1 + bee detector",
        "device": "cpu",
        "imageWidth": image.width,
        "imageHeight": image.height,
    }
