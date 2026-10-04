# HoneyChain — Refined SIH Prototype

## Run backend
```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Swagger: `http://127.0.0.1:8000/docs`

## Run frontend
```powershell
cd frontend
npm install
npm run dev
```

Frontend: `http://localhost:5173`

## Demo accounts
All demo accounts use password `demo123`:
- beekeeper@honeychain.demo
- processor@honeychain.demo
- packaging@honeychain.demo
- admin@honeychain.demo
- consumer@honeychain.demo

## Health workflow
- **Bee Health:** select hive → upload bee image → use live/entered temperature, humidity and weight → run the trained Bee Health CNN + VERRDO EfficientNet + YOLO screening → save result to that hive.
- **Hive Health:** combines saved Bee Health evidence with stored IoT/historical sensor evidence. No health score is fabricated when evidence is unavailable.
- Saved assessments are shown as cards and a selected-hive timeline.

## Blockchain workflow
- Traceability events are mirrored into the SQLite `blockchain_blocks` table as a SHA-256 hash-linked ledger.
- The **Blockchain Trace** page is visible in the **Admin/KVIC** workspace.
- It verifies the complete chain and shows previous hash → block hash links.
- This prototype uses the tamper-evident local ledger; `HONEYCHAIN_FABRIC_GATEWAY_URL` is the production integration point for Hyperledger Fabric.

## QR / consumer workflow
- Packaging workspace → **QR & Digital Passports** → select **Hive first** → select a batch belonging to that hive → Generate QR.
- The page shows the actual QR and the public consumer-dashboard link.
- QR opens `/consumer/{batchId}` and displays the complete hive → harvest → processing → packaging → consumer journey plus blockchain record integrity.

## IoT
Set `HONEYCHAIN_IOT_URL` in `backend/.env` if the sensor device URL differs from the default. The backend performs server-side sensor sync; the browser does not directly call the device.

## Public blockchain deployment

The prototype includes an optional Polygon EVM anchor. By default HoneyChain uses its local SHA-256 hash-linked ledger. To make the ledger public, deploy `blockchain/HoneyChainTraceability.sol` to Polygon Amoy, set the RPC, deployment-wallet private key, contract address and explorer URL in the backend environment, then restart the backend. The admin Blockchain Trace page will show the connected network and contract and can anchor existing local blocks. New traceability events automatically attempt an on-chain anchor.

Use `blockchain/POLYGON_AMOY_SETUP.md` for the deployment sequence. Do not commit a private key.
