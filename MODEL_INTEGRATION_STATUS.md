# HoneyChain model integration

This build keeps the existing Bee Health / VERRDO models and Hive Health evidence pipeline, and adds the trained honey-production model.

## Included model artifacts
- `backend/app/ml/models/bee_health_cnn_best.pth` — Bee Health CNN
- `backend/app/ml/models/efficientnet_b0_varroa_best.pth` — VERRDO EfficientNet
- `backend/app/ml/models/varroa_yolo11n_best.pt` — VERRDO YOLO
- `backend/app/ml/models/bee_final.pt` — recovered from the previously supplied `bee_model_final.zip`; detects `bee` / `blurred_bee` and is included as supplemental visual evidence
- `backend/app/ml/models/honey_production_model.pkl` — trained Random Forest production model
- `backend/app/ml/models/features.pkl` — exact eight-feature order used during training

## Uploaded `best.pt` note
The newly uploaded `best.pt` was a 22-byte empty ZIP container and therefore was not a usable PyTorch/YOLO checkpoint. It was not substituted silently. The previously verified `bee_final.pt` artifact was used instead.

## No static operational data
Operational hive/batch records are not seeded unless `HONEYCHAIN_SEED_DEMO_OPERATIONAL_DATA=true` is explicitly enabled. Hive creation no longer inserts a fake production-history value. Production pages do not fall back to demo predictions.

## Workflow
The complete live workflow remains visible: Hive source → Harvest recorded → Health assessed → Processing verified → Packaging verified → Admin validation → QR issued. Record-integrity verification is attached to each batch instead of exposing a separate Admin Blockchain Trace page.
