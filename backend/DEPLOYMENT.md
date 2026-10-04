# HoneyChain deployment checklist

## 1. Backend
Run the backend on a persistent server/VM because SQLite and the ML checkpoints are local files.

Set:
- `HONEYCHAIN_SECRET` to a long random secret.
- `HONEYCHAIN_DB` to a persistent disk path.
- `HONEYCHAIN_PUBLIC_BASE_URL` to the public HTTPS **frontend** URL. QR codes use this exact URL.
- `FRONTEND_ORIGINS` to the frontend origin(s).
- `HONEYCHAIN_IOT_PULL_ENABLED=true` only when the backend is on the same LAN as the sensor.
- `HONEYCHAIN_IOT_PULL_ENABLED=false` in cloud deployment unless a secure VPN/private network can reach the sensor.
- `HONEYCHAIN_IOT_KEY` and send it as `X-Device-Key` from the IoT device.

## 2. IoT
The address `10.192.35.56` is a private LAN address. A public cloud server cannot normally reach it.

Production pattern:
ESP32 / gateway -> `POST https://YOUR-BACKEND/api/iot/readings` every 15 seconds -> SQLite -> Hive Health.

Payload:
```json
{"hive_id":"HC-001","timestamp":"2026-09-29T12:00:00Z","temperature":34.1,"humidity":62.4}
```

CSV import is also available at `POST /api/iot/csv/{hive_code}` for backfills.

## 3. Blockchain
HoneyChain always maintains a local SHA-256 linked ledger. For deployment, configure the EVM anchor:
- `HONEYCHAIN_BLOCKCHAIN_RPC_URL`
- `HONEYCHAIN_BLOCKCHAIN_PRIVATE_KEY`
- `HONEYCHAIN_BLOCKCHAIN_CONTRACT_ADDRESS`
- `HONEYCHAIN_BLOCKCHAIN_EXPLORER_URL`

Deploy `blockchain/HoneyChainTraceability.sol` using `blockchain/deploy.py` first. A hosted RPC provider may require an API key in its RPC URL; HoneyChain itself does not require an API token.

Do not expose the private key to React or commit it to git.

## 4. QR
Set `HONEYCHAIN_PUBLIC_BASE_URL` to the same public frontend URL before generating QR codes. A QR contains a normal HTTPS URL such as `https://honeychain.example.com/consumer/HC-BATCH-AB12CD34`, so any phone/device can scan it without being paired to the HoneyChain server or another device.

QRs are batch-specific. Changing the selected batch loads its QR automatically; if none exists, HoneyChain creates it.

## 5. Health models
The supplied project contains trained bee-image checkpoints (Bee Health CNN + VERRDO EfficientNet + VERRDO YOLO). The supplied Hive Health implementation is the transparent sensor-history analysis engine, not a separately saved trained image checkpoint. Therefore Hive Health uses actual IoT history and the latest bee-health evidence; the app does not pretend that a hive photograph is a trained hive-health model.
