# HoneyChain production blockchain anchor

HoneyChain uses a live workflow state machine backed by SQLite and a SHA-256 hash-linked ledger. Every meaningful workflow transition creates a ledger block. When Polygon is configured, the same block hash is anchored to the deployed smart contract.

## Workflow states

Hive created → Health assessed → Harvest batch created → Processing recorded → Processing verified → Packaging recorded → Packaging verified → Admin validation → QR issued → Consumer verification.

A batch cannot skip these transitions. A trust score is **pending** until the required health, processing, packaging and admin-validation stages are complete.

## What gets anchored

- Hive registration
- IoT readings / CSV imports
- Health analysis
- Hive source and harvest records
- Processing record and processor verification
- Packaging record and packaging verification
- Admin validation
- QR issuance
- Consumer verification

The application stores business data locally; only cryptographic event proofs should be anchored publicly.

## Polygon deployment

1. Deploy `HoneyChainTraceability.sol` to Polygon Amoy for the prototype or Polygon PoS for production.
2. Configure:
   - `HONEYCHAIN_BLOCKCHAIN_RPC_URL`
   - `HONEYCHAIN_BLOCKCHAIN_PRIVATE_KEY`
   - `HONEYCHAIN_BLOCKCHAIN_CONTRACT_ADDRESS`
   - `HONEYCHAIN_BLOCKCHAIN_EXPLORER_URL`
3. Restart the backend.
4. Open **Admin → Blockchain Trace**. The page reads the live ledger and shows block hashes, Polygon transaction hashes and explorer links when anchoring succeeds.

No HoneyChain application API token is required. A hosted RPC provider may require credentials in its RPC URL. Never expose the wallet private key to the frontend.

## IoT deployment

The private LAN address `10.192.35.56` cannot be reached by a cloud deployment. For production, configure the ESP32/device to POST every 15 seconds to:

`POST https://YOUR-BACKEND/api/iot/readings`

with JSON:

`{ "hive_id": "HC-001", "timestamp": "...", "temperature": 34.1, "humidity": 62.4 }`

For local development, HoneyChain can pull from `HONEYCHAIN_IOT_URL`.
