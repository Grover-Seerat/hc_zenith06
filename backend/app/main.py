from fastapi import (
    FastAPI,
    HTTPException,
    Depends,
    UploadFile,
    File,
    Form,
    Header,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import sqlite3
import time
import hashlib
import secrets
import json
import os
import urllib.request
import urllib.error
import re
import io

# Heavy integrations are loaded lazily. This keeps normal API startup small
# enough for low-memory deployment targets such as a 512 MB container.
Web3 = None

from .ml.hive_analysis import analyze_hive


def predict_image(raw):
    from .ml.inference import predict_image as _predict_image
    return _predict_image(raw)


def predict_honey_production(*args, **kwargs):
    from .ml.production import predict_honey_production as _predict_honey_production
    return _predict_honey_production(*args, **kwargs)


def production_model_info():
    from .ml.production import model_info as _model_info
    return _model_info()


def _get_web3():
    global Web3
    if Web3 is None:
        try:
            from web3 import Web3 as _Web3
            Web3 = _Web3
        except Exception:
            return None
    return Web3


# ============================================================
# CONFIGURATION
# ============================================================

BASE = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("HONEYCHAIN_DB", BASE / "honeychain.db"))

SECRET = os.getenv(
    "HONEYCHAIN_SECRET",
    "honeychain-demo-secret"
)

IOT_DEVICE_URL = os.getenv('HONEYCHAIN_IOT_URL', 'http://10.192.35.56/')
IOT_PULL_ENABLED = os.getenv('HONEYCHAIN_IOT_PULL_ENABLED', 'true').lower() in ('1','true','yes','on')
FABRIC_GATEWAY_URL = os.getenv('HONEYCHAIN_FABRIC_GATEWAY_URL', '').strip()
PUBLIC_BASE_URL = os.getenv('HONEYCHAIN_PUBLIC_BASE_URL', 'http://localhost:5173').rstrip('/')
BLOCKCHAIN_RPC_URL = os.getenv('HONEYCHAIN_BLOCKCHAIN_RPC_URL', '').strip()
BLOCKCHAIN_PRIVATE_KEY = os.getenv('HONEYCHAIN_BLOCKCHAIN_PRIVATE_KEY', '').strip()
BLOCKCHAIN_CONTRACT_ADDRESS = os.getenv('HONEYCHAIN_BLOCKCHAIN_CONTRACT_ADDRESS', '').strip()
BLOCKCHAIN_EXPLORER_URL = os.getenv('HONEYCHAIN_BLOCKCHAIN_EXPLORER_URL', '').strip().rstrip('/')
SEED_DEMO_OPERATIONAL_DATA = os.getenv('HONEYCHAIN_SEED_DEMO_OPERATIONAL_DATA', 'false').lower() in ('1','true','yes','on')

ORIGINS = [
    x.strip()
    for x in os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")
]


# ============================================================
# APP
# ============================================================

app = FastAPI(
    title="HoneyChain API",
    version="3.0.0",
    description="HoneyChain Smart Beekeeping and Honey Traceability API"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)


# ============================================================
# DATABASE
# ============================================================

# SQLite is used for the free Render deployment.  Render can receive
# overlapping requests, so the connection needs an explicit busy timeout
# and WAL mode instead of SQLite's very short default lock timeout.
DB_BUSY_TIMEOUT_MS = int(os.getenv("HONEYCHAIN_DB_BUSY_TIMEOUT_MS", "30000"))
DB_LOCK_RETRIES = int(os.getenv("HONEYCHAIN_DB_LOCK_RETRIES", "4"))


class ResilientSQLiteConnection(sqlite3.Connection):
    """SQLite connection that tolerates short-lived writer contention."""

    @staticmethod
    def _locked(exc):
        return "database is locked" in str(exc).lower()

    def _retry_delay(self, attempt):
        time.sleep(min(0.25 * (2 ** attempt), 2.0))

    def execute(self, sql, parameters=()):
        for attempt in range(DB_LOCK_RETRIES + 1):
            try:
                return super().execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if not self._locked(exc) or attempt >= DB_LOCK_RETRIES:
                    raise
                self._retry_delay(attempt)

    def executemany(self, sql, parameters):
        for attempt in range(DB_LOCK_RETRIES + 1):
            try:
                return super().executemany(sql, parameters)
            except sqlite3.OperationalError as exc:
                if not self._locked(exc) or attempt >= DB_LOCK_RETRIES:
                    raise
                self._retry_delay(attempt)

    def executescript(self, sql_script):
        for attempt in range(DB_LOCK_RETRIES + 1):
            try:
                return super().executescript(sql_script)
            except sqlite3.OperationalError as exc:
                if not self._locked(exc) or attempt >= DB_LOCK_RETRIES:
                    raise
                self._retry_delay(attempt)

    def commit(self):
        for attempt in range(DB_LOCK_RETRIES + 1):
            try:
                return super().commit()
            except sqlite3.OperationalError as exc:
                if not self._locked(exc) or attempt >= DB_LOCK_RETRIES:
                    raise
                self._retry_delay(attempt)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        str(DB_PATH),
        timeout=max(DB_BUSY_TIMEOUT_MS / 1000.0, 1.0),
        check_same_thread=False,
        factory=ResilientSQLiteConnection
    )

    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys=ON")

    return connection


def rows(connection, query, args=()):
    return [
        dict(x)
        for x in connection.execute(query, args).fetchall()
    ]


def row(connection, query, args=()):
    result = connection.execute(
        query,
        args
    ).fetchone()

    return dict(result) if result else None


# ============================================================
# SECURITY / HASHING
# ============================================================

def password_hash(password: str):
    return hashlib.sha256(
        (SECRET + password).encode()
    ).hexdigest()


def create_event_hash(payload, previous_hash=None):
    data = {
        **payload,
        "previous_hash": previous_hash or "GENESIS"
    }

    return hashlib.sha256(
        json.dumps(
            data,
            sort_keys=True,
            separators=(",", ":")
        ).encode()
    ).hexdigest()


# ============================================================
# BLOCKCHAIN / IMMUTABLE TRACEABILITY LEDGER
# ============================================================

def _evm_client():
    web3_cls = _get_web3()
    if not (web3_cls and BLOCKCHAIN_RPC_URL and BLOCKCHAIN_PRIVATE_KEY and BLOCKCHAIN_CONTRACT_ADDRESS):
        return None
    try:
        w3 = web3_cls(web3_cls.HTTPProvider(BLOCKCHAIN_RPC_URL, request_kwargs={"timeout": 8}))
        if not w3.is_connected():
            return None
        return w3
    except Exception:
        return None


def _onchain_record(block_hash: str, batch_id: str, event_type: str):
    """Anchor one HoneyChain block to the configured EVM network."""
    w3 = _evm_client()
    if not w3:
        return None
    try:
        abi = [{"inputs":[{"internalType":"bytes32","name":"eventHash","type":"bytes32"},{"internalType":"string","name":"batchId","type":"string"},{"internalType":"string","name":"eventType","type":"string"}],"name":"recordEvent","outputs":[],"stateMutability":"nonpayable","type":"function"}]
        web3_cls = _get_web3()
        contract = w3.eth.contract(address=web3_cls.to_checksum_address(BLOCKCHAIN_CONTRACT_ADDRESS), abi=abi)
        account = w3.eth.account.from_key(BLOCKCHAIN_PRIVATE_KEY)
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        call = contract.functions.recordEvent(bytes.fromhex(block_hash), batch_id, event_type)
        gas_estimate = call.estimate_gas({'from': account.address})
        gas_price = w3.eth.gas_price
        tx = call.build_transaction({
            'from': account.address, 'nonce': nonce, 'chainId': w3.eth.chain_id,
            'gas': int(gas_estimate * 1.20),
            'maxFeePerGas': int(gas_price * 2),
            'maxPriorityFeePerGas': max(w3.to_wei('0.1', 'gwei'), int(gas_price // 10))
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
    except Exception:
        # Blockchain anchoring must never prevent sensor/ML/traceability writes.
        return None


def add_blockchain_block(connection, batch_id, event_id, payload, level=None):
    previous = connection.execute(
        "SELECT block_hash FROM blockchain_blocks ORDER BY block_index DESC LIMIT 1"
    ).fetchone()
    previous_hash = previous["block_hash"] if previous else "GENESIS"
    next_index = connection.execute(
        "SELECT COALESCE(MAX(block_index), 0) + 1 AS n FROM blockchain_blocks"
    ).fetchone()["n"]
    event_level = level or payload.get("level") or payload.get("event_type") or "Traceability"
    canonical = {
        "block_index": next_index, "batch_id": batch_id, "event_id": event_id,
        "level": event_level, "payload": payload, "previous_hash": previous_hash
    }
    block_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    tx_hash = _onchain_record(block_hash, batch_id, str(event_level))
    connection.execute(
        """INSERT INTO blockchain_blocks
        (block_index,batch_id,event_id,payload,previous_hash,block_hash,level,onchain_tx_hash)
        VALUES (?,?,?,?,?,?,?,?)""",
        (next_index, batch_id, event_id, json.dumps(payload, sort_keys=True), previous_hash, block_hash, event_level, tx_hash)
    )
    return block_hash


def verify_blockchain_chain(connection, batch_id=None):
    all_blocks = rows(connection, "SELECT * FROM blockchain_blocks ORDER BY block_index")
    expected_prev = "GENESIS"
    valid = True
    for b in all_blocks:
        payload = json.loads(b["payload"])
        canonical = {
            "block_index": b["block_index"], "batch_id": b["batch_id"], "event_id": b["event_id"],
            "level": b.get("level") or payload.get("level") or payload.get("event_type") or "Traceability",
            "payload": payload, "previous_hash": expected_prev
        }
        expected_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if b["previous_hash"] != expected_prev or b["block_hash"] != expected_hash:
            valid = False
            break
        expected_prev = b["block_hash"]
    blocks = [b for b in all_blocks if not batch_id or b["batch_id"] == batch_id]
    return valid, blocks


def blockchain_mirror_status():
    configured = bool(BLOCKCHAIN_RPC_URL and BLOCKCHAIN_PRIVATE_KEY and BLOCKCHAIN_CONTRACT_ADDRESS)
    chain_id = None
    wallet = None
    network = None
    if configured:
        try:
            w3 = _evm_client()
            if w3:
                chain_id = w3.eth.chain_id
                wallet = w3.eth.account.from_key(BLOCKCHAIN_PRIVATE_KEY).address
                network = {80002: 'Polygon Amoy Testnet', 137: 'Polygon PoS Mainnet'}.get(chain_id, f'EVM chain {chain_id}')
        except Exception:
            pass
    return {
        "mode": "Public EVM blockchain anchor + local hash chain" if configured else "HoneyChain tamper-evident hash-linked ledger",
        "chainConfigured": configured,
        "chainId": chain_id,
        "network": network,
        "contractAddress": BLOCKCHAIN_CONTRACT_ADDRESS or None,
        "anchorWallet": wallet,
        "fabricConfigured": False,
        "explorerUrl": BLOCKCHAIN_EXPLORER_URL or None
    }

# ============================================================
# DATABASE INITIALIZATION
# ============================================================

def init_db():

    connection = db()

    connection.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            organization TEXT,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS hives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_code TEXT UNIQUE NOT NULL,
            beekeeper_id INTEGER,
            location TEXT,
            latitude REAL,
            longitude REAL,
            status TEXT DEFAULT 'active',
            species TEXT DEFAULT 'Apis mellifera',
            colony_strength TEXT DEFAULT 'Healthy',

            temperature REAL DEFAULT 34.2,
            humidity REAL DEFAULT 62.0,
            weight REAL DEFAULT 45.5,
            activity TEXT DEFAULT 'High',
            image_risk TEXT DEFAULT 'Low',

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY(beekeeper_id)
                REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS sensor_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER NOT NULL,
            day TEXT,
            temperature REAL,
            humidity REAL,
            weight REAL
        );

        CREATE TABLE IF NOT EXISTS production_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER NOT NULL,
            period TEXT,
            production REAL
        );

        CREATE TABLE IF NOT EXISTS production_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER NOT NULL,
            prediction REAL NOT NULL,
            prediction_date TEXT NOT NULL,
            environmental_temperature REAL,
            relative_humidity REAL,
            hive_temperature REAL,
            hive_humidity REAL,
            wind_speed REAL,
            total_hive_weight REAL,
            historical_average REAL,
            change_percent REAL,
            risk_level TEXT,
            reasons TEXT,
            recommended_actions TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(hive_id) REFERENCES hives(id)
        );

        CREATE TABLE IF NOT EXISTS batches (
            batch_id TEXT PRIMARY KEY,
            hive_id INTEGER,
            honey_type TEXT,
            quantity REAL,
            harvest_date TEXT,
            origin TEXT,
            status TEXT,
            created_at TEXT,

            integrity_hash TEXT,
            previous_hash TEXT
        );

        CREATE TABLE IF NOT EXISTS traceability_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            event_type TEXT,
            actor TEXT,
            actor_id INTEGER,
            event_date TEXT,
            description TEXT,
            metadata TEXT,

            previous_hash TEXT,
            event_hash TEXT,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER,
            temperature REAL,
            humidity REAL,
            weight REAL,
            battery_level REAL,
            signal_strength REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS iot_sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            temperature REAL NOT NULL,
            humidity REAL NOT NULL,
            received_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(hive_id) REFERENCES hives(id)
        );

        CREATE TABLE IF NOT EXISTS ai_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER,
            batch_id TEXT,
            model_name TEXT,
            model_version TEXT,
            prediction TEXT,
            confidence REAL,
            risk_level TEXT,
            severity TEXT,
            explanation TEXT,
            recommendations TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS health_analysis_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hive_id INTEGER NOT NULL,
            image_name TEXT,
            bee_status TEXT,
            bee_health_score REAL,
            bee_class TEXT,
            varroa_risk TEXT,
            varroa_probability REAL,
            mite_count INTEGER DEFAULT 0,
            sensor_score REAL,
            hive_score REAL,
            hive_status TEXT,
            temperature REAL,
            humidity REAL,
            weight REAL,
            visual_model TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(hive_id) REFERENCES hives(id)
        );

        CREATE TABLE IF NOT EXISTS qr_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT UNIQUE,
            qr_token TEXT UNIQUE,
            public_url TEXT,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS blockchain_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_index INTEGER UNIQUE NOT NULL,
            batch_id TEXT NOT NULL,
            event_id INTEGER,
            payload TEXT NOT NULL,
            previous_hash TEXT NOT NULL,
            block_hash TEXT UNIQUE NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    # Lightweight schema migration for deployed SQLite databases.
    existing_cols = {r["name"] for r in connection.execute("PRAGMA table_info(blockchain_blocks)").fetchall()}
    if "level" not in existing_cols:
        connection.execute("ALTER TABLE blockchain_blocks ADD COLUMN level TEXT")
    if "onchain_tx_hash" not in existing_cols:
        connection.execute("ALTER TABLE blockchain_blocks ADD COLUMN onchain_tx_hash TEXT")

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    users = [
        (
            1,
            "beekeeper@honeychain.demo",
            "Demo Beekeeper",
            "beekeeper",
            "HoneyChain Beekeeping Network"
        ),
        (
            2,
            "processor@honeychain.demo",
            "Demo Processor",
            "processor",
            "HoneyChain Processing Unit"
        ),
        (
            3,
            "packaging@honeychain.demo",
            "Demo Packaging Unit",
            "packaging",
            "HoneyChain Packaging Unit"
        ),
        (
            4,
            "admin@honeychain.demo",
            "KVIC Authority Demo",
            "admin",
            "KVIC Authority"
        ),
        (
            5,
            "consumer@honeychain.demo",
            "Demo Consumer",
            "consumer",
            "HoneyChain Consumer"
        ),
    ]

    for user in users:

        connection.execute(
            """
            INSERT INTO users
            (
                id,
                email,
                password_hash,
                name,
                role,
                organization
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(email)
            DO UPDATE SET
                password_hash = excluded.password_hash,
                name = excluded.name,
                role = excluded.role,
                organization = excluded.organization
            """,
            (
                user[0],
                user[1],
                password_hash("demo123"),
                user[2],
                user[3],
                user[4]
            )
        )

    # Operational records are created through workflow actions.
    # Set HONEYCHAIN_SEED_DEMO_OPERATIONAL_DATA=true only for a demo dataset.
    if not SEED_DEMO_OPERATIONAL_DATA:
        connection.commit()
        connection.close()
        return

    # --------------------------------------------------------
    # HIVE DATA
    # --------------------------------------------------------

    hives = [
        (
            "HC-001",
            1,
            "Chandigarh",
            30.7333,
            76.7794,
            "active",
            "Apis mellifera",
            "Strong",
            34.2,
            62.0,
            45.8,
            "High",
            "Low"
        ),
        (
            "HC-002",
            1,
            "Mohali",
            30.7046,
            76.7179,
            "active",
            "Apis mellifera",
            "Healthy",
            33.7,
            66.0,
            41.6,
            "High",
            "Low"
        ),
        (
            "HC-003",
            1,
            "Panchkula",
            30.6942,
            76.8606,
            "active",
            "Apis cerana",
            "Healthy",
            35.1,
            58.0,
            48.2,
            "Medium",
            "Medium"
        ),
    ]

    for hive in hives:

        existing = connection.execute(
            "SELECT id FROM hives WHERE hive_code = ?",
            (hive[0],)
        ).fetchone()

        if existing:

            connection.execute(
                """
                UPDATE hives SET

                    beekeeper_id = ?,
                    location = ?,
                    latitude = ?,
                    longitude = ?,
                    status = ?,
                    species = ?,
                    colony_strength = ?,
                    temperature = ?,
                    humidity = ?,
                    weight = ?,
                    activity = ?,
                    image_risk = ?

                WHERE hive_code = ?
                """,
                (
                    hive[1],
                    hive[2],
                    hive[3],
                    hive[4],
                    hive[5],
                    hive[6],
                    hive[7],
                    hive[8],
                    hive[9],
                    hive[10],
                    hive[11],
                    hive[12],
                    hive[0]
                )
            )

        else:

            connection.execute(
                """
                INSERT INTO hives
                (
                    hive_code,
                    beekeeper_id,
                    location,
                    latitude,
                    longitude,
                    status,
                    species,
                    colony_strength,
                    temperature,
                    humidity,
                    weight,
                    activity,
                    image_risk
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                hive
            )

    connection.commit()

    # --------------------------------------------------------
    # GET HIVE DATABASE IDS
    # --------------------------------------------------------

    hive_map = {}

    for hive_code in ["HC-001", "HC-002", "HC-003"]:

        h = connection.execute(
            "SELECT id FROM hives WHERE hive_code = ?",
            (hive_code,)
        ).fetchone()

        if h:
            hive_map[hive_code] = h["id"]

    # --------------------------------------------------------
    # SENSOR HISTORY
    # --------------------------------------------------------

    sensor_seed = {
        "HC-001": [
            ("Day 1", 33.4, 61, 43.2),
            ("Day 2", 33.8, 62, 44.0),
            ("Day 3", 34.1, 63, 44.7),
            ("Day 4", 34.2, 62, 45.8),
        ],
        "HC-002": [
            ("Day 1", 32.8, 64, 39.8),
            ("Day 2", 33.2, 65, 40.5),
            ("Day 3", 33.5, 66, 41.0),
            ("Day 4", 33.7, 66, 41.6),
        ],
        "HC-003": [
            ("Day 1", 34.4, 56, 46.1),
            ("Day 2", 34.8, 57, 46.9),
            ("Day 3", 35.0, 58, 47.5),
            ("Day 4", 35.1, 58, 48.2),
        ],
    }

    for hive_code, history in sensor_seed.items():

        hive_id = hive_map.get(hive_code)

        if not hive_id:
            continue

        count = connection.execute(
            """
            SELECT COUNT(*)
            FROM sensor_history
            WHERE hive_id = ?
            """,
            (hive_id,)
        ).fetchone()[0]

        if count == 0:

            connection.executemany(
                """
                INSERT INTO sensor_history
                (
                    hive_id,
                    day,
                    temperature,
                    humidity,
                    weight
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        hive_id,
                        period,
                        temperature,
                        humidity,
                        weight
                    )
                    for period,
                    temperature,
                    humidity,
                    weight in history
                ]
            )

    # --------------------------------------------------------
    # PRODUCTION HISTORY
    # --------------------------------------------------------

    production_seed = {
        "HC-001": [
            ("Week 1", 18.2),
            ("Week 2", 19.5),
            ("Week 3", 21.1),
            ("Week 4", 17.9),
        ],
        "HC-002": [
            ("Week 1", 15.4),
            ("Week 2", 16.8),
            ("Week 3", 18.2),
            ("Week 4", 16.1),
        ],
        "HC-003": [
            ("Week 1", 20.1),
            ("Week 2", 21.4),
            ("Week 3", 22.3),
            ("Week 4", 19.8),
        ],
    }

    for hive_code, history in production_seed.items():

        hive_id = hive_map.get(hive_code)

        if not hive_id:
            continue

        count = connection.execute(
            """
            SELECT COUNT(*)
            FROM production_history
            WHERE hive_id = ?
            """,
            (hive_id,)
        ).fetchone()[0]

        if count == 0:

            connection.executemany(
                """
                INSERT INTO production_history
                (
                    hive_id,
                    period,
                    production
                )
                VALUES (?, ?, ?)
                """,
                [
                    (
                        hive_id,
                        period,
                        production
                    )
                    for period, production in history
                ]
            )

    # --------------------------------------------------------
    # BATCHES
    # --------------------------------------------------------

    batch_data = [
        (
            "HC-BATCH-001",
            "HC-001",
            "Mustard Honey",
            24.5,
            "2026-09-05",
            "Chandigarh",
            "Verified"
        ),
        (
            "HC-BATCH-002",
            "HC-002",
            "Multifloral Honey",
            19.2,
            "2026-09-06",
            "Mohali",
            "Verified"
        ),
        (
            "HC-BATCH-003",
            "HC-003",
            "Eucalyptus Honey",
            27.8,
            "2026-09-07",
            "Panchkula",
            "Processing"
        ),
    ]

    for batch in batch_data:

        batch_id = batch[0]

        exists = connection.execute(
            """
            SELECT batch_id
            FROM batches
            WHERE batch_id = ?
            """,
            (batch_id,)
        ).fetchone()

        if not exists:

            hive_id = hive_map.get(batch[1])

            previous = connection.execute(
                """
                SELECT integrity_hash
                FROM batches
                ORDER BY created_at DESC, batch_id DESC
                LIMIT 1
                """
            ).fetchone()

            previous_hash = (
                previous["integrity_hash"]
                if previous
                else None
            )

            payload = {
                "batch_id": batch[0],
                "hive_id": batch[1],
                "honey_type": batch[2],
                "quantity": batch[3],
                "harvest_date": batch[4],
                "origin": batch[5],
            }

            integrity_hash = create_event_hash(
                payload,
                previous_hash
            )

            connection.execute(
                """
                INSERT INTO batches
                (
                    batch_id,
                    hive_id,
                    honey_type,
                    quantity,
                    harvest_date,
                    origin,
                    status,
                    created_at,
                    integrity_hash,
                    previous_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch[0],
                    hive_id,
                    batch[2],
                    batch[3],
                    batch[4],
                    batch[5],
                    batch[6],
                    batch[4],
                    integrity_hash,
                    previous_hash
                )
            )

    connection.commit()

    # --------------------------------------------------------
    # TRACEABILITY EVENTS
    # --------------------------------------------------------

    event_count = connection.execute(
        "SELECT COUNT(*) FROM traceability_events"
    ).fetchone()[0]

    if event_count == 0:

        events = [
            (
                "HC-BATCH-001",
                "Hive source",
                "Beekeeper",
                1,
                "2026-09-05",
                "Source hive HC-001 recorded.",
            ),
            (
                "HC-BATCH-001",
                "Harvest recorded",
                "Beekeeper",
                1,
                "2026-09-05",
                "Harvest quantity and honey type recorded.",
            ),
            (
                "HC-BATCH-001",
                "Processing",
                "Processor",
                2,
                "2026-09-08",
                "Processing event linked to batch.",
            ),
            (
                "HC-BATCH-001",
                "Packaging",
                "Packaging Unit",
                3,
                "2026-09-09",
                "Packaging record linked to batch.",
            ),
            (
                "HC-BATCH-001",
                "Consumer verification",
                "Consumer",
                5,
                "2026-09-10",
                "Public passport available through QR.",
            ),

            (
                "HC-BATCH-002",
                "Hive source",
                "Beekeeper",
                1,
                "2026-09-06",
                "Source hive HC-002 recorded.",
            ),
            (
                "HC-BATCH-002",
                "Harvest recorded",
                "Beekeeper",
                1,
                "2026-09-06",
                "Harvest quantity recorded.",
            ),
            (
                "HC-BATCH-002",
                "Processing",
                "Processor",
                2,
                "2026-09-08",
                "Processing event linked to batch.",
            ),

            (
                "HC-BATCH-003",
                "Hive source",
                "Beekeeper",
                1,
                "2026-09-07",
                "Source hive HC-003 recorded.",
            ),
            (
                "HC-BATCH-003",
                "Harvest recorded",
                "Beekeeper",
                1,
                "2026-09-07",
                "Harvest quantity recorded.",
            ),
        ]

        previous = None

        for event in events:

            payload = {
                "batch_id": event[0],
                "event_type": event[1],
                "actor": event[2],
                "event_date": event[4],
                "description": event[5],
            }

            event_hash = create_event_hash(
                payload,
                previous
            )

            connection.execute(
                """
                INSERT INTO traceability_events
                (
                    batch_id,
                    event_type,
                    actor,
                    actor_id,
                    event_date,
                    description,
                    metadata,
                    previous_hash,
                    event_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event[0],
                    event[1],
                    event[2],
                    event[3],
                    event[4],
                    event[5],
                    "{}",
                    previous,
                    event_hash
                )
            )

            previous = event_hash

    # Backfill the blockchain ledger for traceability records created by older versions.
    # Also guarantee that every existing batch has at least one live ledger block.
    existing_blocks = connection.execute("SELECT COUNT(*) AS n FROM blockchain_blocks").fetchone()["n"]
    if existing_blocks == 0:
        legacy_events = connection.execute(
            "SELECT id,batch_id,event_type,actor,event_date,description,metadata "
            "FROM traceability_events ORDER BY id"
        ).fetchall()
        for ev in legacy_events:
            try:
                metadata = json.loads(ev["metadata"] or "{}")
            except Exception:
                metadata = {}
            add_blockchain_block(connection, ev["batch_id"], ev["id"], {
                "event_type": ev["event_type"],
                "actor": ev["actor"],
                "event_date": ev["event_date"],
                "description": ev["description"],
                "metadata": metadata
            })

    # Self-heal partially initialized deployments: if a batch exists but has no
    # blockchain block, create a real database-backed "Batch Created" block.
    all_batches = connection.execute(
        "SELECT batch_id,hive_id,honey_type,quantity,harvest_date,origin,status FROM batches"
    ).fetchall()
    for batch_row in all_batches:
        has_block = connection.execute(
            "SELECT 1 FROM blockchain_blocks WHERE batch_id=? LIMIT 1",
            (batch_row["batch_id"],)
        ).fetchone()
        if not has_block:
            add_blockchain_block(
                connection,
                batch_row["batch_id"],
                None,
                {
                    "event_type": "Batch Created",
                    "level": "Batch",
                    "event_date": batch_row["harvest_date"],
                    "description": f"Production batch {batch_row['batch_id']} registered.",
                    "hive_id": batch_row["hive_id"],
                    "honey_type": batch_row["honey_type"],
                    "quantity": batch_row["quantity"],
                    "origin": batch_row["origin"],
                    "status": batch_row["status"]
                },
                level="Batch"
            )

    connection.commit()
    connection.close()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():

    # Enable WAL once at startup. WAL lets readers continue while a writer
    # commits and is substantially safer for the concurrent Render workload.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    startup_connection = sqlite3.connect(
        str(DB_PATH),
        timeout=max(DB_BUSY_TIMEOUT_MS / 1000.0, 1.0),
        check_same_thread=False
    )
    try:
        startup_connection.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
        startup_connection.execute("PRAGMA journal_mode=WAL")
        startup_connection.execute("PRAGMA synchronous=NORMAL")
        startup_connection.execute("PRAGMA foreign_keys=ON")
    finally:
        startup_connection.close()

    init_db()


# ============================================================
# ROOT
# ============================================================

@app.get("/")
def root():

    return {
        "ok": True,
        "service": "HoneyChain API",
        "database": "SQLite"
    }


@app.get("/health")
def health_check():

    connection = db()

    users = connection.execute(
        "SELECT COUNT(*) FROM users"
    ).fetchone()[0]

    hives = connection.execute(
        "SELECT COUNT(*) FROM hives"
    ).fetchone()[0]

    batches = connection.execute(
        "SELECT COUNT(*) FROM batches"
    ).fetchone()[0]

    connection.close()

    return {
        "ok": True,
        "service": "HoneyChain API",
        "database": "SQLite",
        "users": users,
        "hives": hives,
        "batches": batches
    }


# ============================================================
# AUTH MODELS
# ============================================================

class Login(BaseModel):

    email: str
    password: str


class HiveCreate(BaseModel):

    location: str
    species: str
    colonyStrength: str
    temperature: float
    humidity: float
    weight: float
    activity: str
    imageRisk: str


class BatchCreate(BaseModel):

    hiveId: str
    honeyType: str
    quantity: float = Field(gt=0)
    harvestDate: str
    origin: str


# ============================================================
# AUTH
# ============================================================

def current_user(
    credentials: Optional[
        HTTPAuthorizationCredentials
    ] = Depends(security)
):

    if not credentials:

        raise HTTPException(
            status_code=401,
            detail="Missing bearer token"
        )

    connection = db()

    token_data = row(
        connection,
        """
        SELECT *
        FROM tokens
        WHERE token = ?
        """,
        (credentials.credentials,)
    )

    if not token_data:

        connection.close()

        raise HTTPException(
            status_code=401,
            detail="Invalid token"
        )

    if datetime.fromisoformat(
        token_data["expires_at"]
    ) < datetime.utcnow():

        connection.close()

        raise HTTPException(
            status_code=401,
            detail="Token expired"
        )

    user = row(
        connection,
        """
        SELECT
            id,
            email,
            name,
            role,
            organization
        FROM users
        WHERE id = ?
        """,
        (token_data["user_id"],)
    )

    connection.close()

    if not user:

        raise HTTPException(
            status_code=401,
            detail="User not found"
        )

    return user

# ============================================================
# BLOCKCHAIN + QR API
# ============================================================

@app.get("/api/batches/{batch_id}/blockchain")
@app.get("/batches/{batch_id}/blockchain")
def get_blockchain(batch_id: str, user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Blockchain Trace is available to the admin workspace")
    connection = db()
    batch = row(connection, "SELECT batch_id FROM batches WHERE batch_id=?", (batch_id,))
    if not batch:
        connection.close()
        raise HTTPException(status_code=404, detail="Batch not found")
    valid, blocks = verify_blockchain_chain(connection, batch_id)
    connection.close()
    return {
        "batchId": batch_id,
        "verified": valid,
        "blockCount": len(blocks),
        "blocks": blocks,
        **blockchain_mirror_status()
    }


@app.get("/api/blockchain/status")
def blockchain_status(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Blockchain status is available to the admin workspace")
    return blockchain_mirror_status()


@app.post("/api/blockchain/anchor-existing")
def anchor_existing_blocks(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admin can anchor existing records")
    if not blockchain_mirror_status()["chainConfigured"]:
        raise HTTPException(status_code=503, detail="Public blockchain is not configured. Set the Polygon RPC, deployment wallet private key and contract address first.")
    connection = db()
    blocks = rows(connection, "SELECT block_index,batch_id,level,block_hash,onchain_tx_hash FROM blockchain_blocks WHERE onchain_tx_hash IS NULL ORDER BY block_index")
    results = []
    for block in blocks:
        tx_hash = _onchain_record(block["block_hash"], block["batch_id"], block["level"] or "Traceability")
        if tx_hash:
            connection.execute("UPDATE blockchain_blocks SET onchain_tx_hash=? WHERE block_index=?", (tx_hash, block["block_index"]))
            results.append({"blockIndex": block["block_index"], "batchId": block["batch_id"], "txHash": tx_hash})
    connection.commit()
    connection.close()
    return {"anchored": len(results), "results": results, **blockchain_mirror_status()}


@app.get("/api/blockchain/overview")
def blockchain_overview(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Blockchain Trace is available to the admin workspace")
    connection = db()
    valid, blocks = verify_blockchain_chain(connection)
    counts = rows(connection, "SELECT COALESCE(level,'Traceability') AS level, COUNT(*) AS count FROM blockchain_blocks GROUP BY COALESCE(level,'Traceability') ORDER BY MIN(block_index)")
    connection.close()
    return {"verified": valid, "blockCount": len(blocks), "blocks": blocks, "levels": counts, **blockchain_mirror_status()}

@app.get("/api/qr/{batch_id}")
def qr_api(batch_id: str, user=Depends(current_user)):
    connection = db()
    qr = row(connection, "SELECT * FROM qr_codes WHERE batch_id=?", (batch_id,))
    connection.close()
    if not qr:
        raise HTTPException(status_code=404, detail="QR not generated for this batch")
    return {
        **qr,
        "qrUrl": qr["public_url"],
        "dashboardUrl": qr["public_url"],
        "qrSvg": build_qr_svg(qr["public_url"]),
        "qrDataUrl": build_qr_data_url(qr["public_url"])
    }


def require(*roles):

    def dependency(
        user=Depends(current_user)
    ):

        if user["role"] not in roles:

            raise HTTPException(
                status_code=403,
                detail="This workspace is not available to your role"
            )

        return user

    return dependency


@app.post("/auth/login")
def login(credentials: Login):

    email = credentials.email.strip().lower()
    password = credentials.password

    connection = db()

    # Self-healing demo accounts
    demo_accounts = {

        "beekeeper@honeychain.demo": (
            1,
            "Demo Beekeeper",
            "beekeeper",
            "HoneyChain Beekeeping Network"
        ),

        "processor@honeychain.demo": (
            2,
            "Demo Processor",
            "processor",
            "HoneyChain Processing Unit"
        ),

        "packaging@honeychain.demo": (
            3,
            "Demo Packaging Unit",
            "packaging",
            "HoneyChain Packaging Unit"
        ),

        "admin@honeychain.demo": (
            4,
            "KVIC Authority Demo",
            "admin",
            "KVIC Authority"
        ),

        "consumer@honeychain.demo": (
            5,
            "Demo Consumer",
            "consumer",
            "HoneyChain Consumer"
        ),
    }

    if (
        email in demo_accounts
        and password == "demo123"
    ):

        user_id, name, role, organization = (
            demo_accounts[email]
        )

        connection.execute(
            """
            INSERT INTO users
            (
                id,
                email,
                password_hash,
                name,
                role,
                organization
            )
            VALUES (?, ?, ?, ?, ?, ?)

            ON CONFLICT(email)
            DO UPDATE SET
                password_hash = excluded.password_hash,
                name = excluded.name,
                role = excluded.role,
                organization = excluded.organization
            """,
            (
                user_id,
                email,
                password_hash("demo123"),
                name,
                role,
                organization
            )
        )

        connection.commit()

    user = row(
        connection,
        """
        SELECT *
        FROM users
        WHERE lower(trim(email)) = ?
        """,
        (email,)
    )

    if (
        not user
        or user["password_hash"]
        != password_hash(password)
    ):

        connection.close()

        raise HTTPException(
            status_code=401,
            detail="Invalid email or password"
        )

    token = secrets.token_urlsafe(32)

    expires = (
        datetime.utcnow()
        + timedelta(days=7)
    ).isoformat()

    connection.execute(
        """
        INSERT INTO tokens
        (
            token,
            user_id,
            expires_at
        )
        VALUES (?, ?, ?)
        """,
        (
            token,
            user["id"],
            expires
        )
    )

    connection.commit()
    connection.close()

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "organization": user["organization"]
        }
    }


@app.get("/me")
def me(
    user=Depends(current_user)
):

    return user


# ============================================================
# DASHBOARD
# ============================================================

@app.get("/dashboard")
def dashboard(
    user=Depends(current_user)
):

    connection = db()

    hive_count = connection.execute(
        "SELECT COUNT(*) FROM hives"
    ).fetchone()[0]

    batch_count = connection.execute(
        "SELECT COUNT(*) FROM batches"
    ).fetchone()[0]

    event_count = connection.execute(
        "SELECT COUNT(*) FROM traceability_events"
    ).fetchone()[0]

    connection.close()

    return {
        "user": user,
        "stats": {
            "hives": hive_count,
            "batches": batch_count,
            "traceabilityEvents": event_count
        }
    }


# ============================================================
# HIVE HELPERS
# ============================================================

def calculate_hive_health(hive):

    temperature = hive.get("temperature") or 0
    humidity = hive.get("humidity") or 0
    weight = hive.get("weight") or 0
    image_risk = hive.get("image_risk") or "Low"

    temperature_score = (
        25
        if 33 <= temperature <= 35.5
        else 18
        if 31 <= temperature <= 37
        else 10
    )

    humidity_score = (
        25
        if 50 <= humidity <= 70
        else 15
        if humidity <= 80
        else 8
    )

    weight_score = (
        25
        if weight >= 40
        else 20
        if weight >= 35
        else 12
    )

    risk_score = (
        25
        if image_risk == "Low"
        else 15
        if image_risk == "Medium"
        else 5
    )

    score = (
        temperature_score
        + humidity_score
        + weight_score
        + risk_score
    )

    status = (
        "Healthy"
        if score >= 75
        else "Monitor"
        if score >= 60
        else "Needs Attention"
    )

    reasons = []

    if not 33 <= temperature <= 35.5:

        reasons.append(
            "Hive temperature is outside the preferred range."
        )

    if not 50 <= humidity <= 70:

        reasons.append(
            "Hive humidity should be monitored."
        )

    if weight < 40:

        reasons.append(
            "Hive weight is below the current production target."
        )

    if image_risk != "Low":

        reasons.append(
            "AI image screening indicates a condition requiring monitoring."
        )

    if not reasons:

        reasons.append(
            "Temperature, humidity, weight and image screening are currently within expected conditions."
        )

    return score, status, reasons


def hive_output(hive):

    score, health_status, reasons = (
        calculate_hive_health(hive)
    )

    weight = hive.get("weight") or 0

    return {
        **hive,
        "healthScore": score,
        "healthStatus": health_status,
        "healthReasons": reasons
    }


# ============================================================
# HIVES
# ============================================================

@app.post("/hives")
def create_hive(
    hive: HiveCreate,
    user=Depends(current_user)
):

    if user["role"] != "beekeeper":
        raise HTTPException(
            status_code=403,
            detail="Only beekeepers can create hives"
        )

    required = {
        "location": hive.location.strip(),
        "species": hive.species.strip(),
        "colonyStrength": hive.colonyStrength.strip(),
        "activity": hive.activity.strip(),
        "imageRisk": hive.imageRisk.strip(),
    }
    if any(not value for value in required.values()):
        raise HTTPException(status_code=422, detail="All hive fields are required")

    connection = db()
    next_number = connection.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 FROM hives"
    ).fetchone()[0]
    hive_code = f"HC-{int(next_number):03d}"

    connection.execute(
        """
        INSERT INTO hives
        (hive_code, beekeeper_id, location, status, species, colony_strength,
         temperature, humidity, weight, activity, image_risk)
        VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            hive_code, user["id"], required["location"], required["species"],
            required["colonyStrength"], hive.temperature, hive.humidity,
            hive.weight, required["activity"], required["imageRisk"]
        )
    )
    hive_db_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]
    today = datetime.utcnow().date().isoformat()
    connection.execute(
        "INSERT INTO sensor_history (hive_id, day, temperature, humidity, weight) VALUES (?, ?, ?, ?, ?)",
        (hive_db_id, today, hive.temperature, hive.humidity, hive.weight)
    )
    add_blockchain_block(connection, f"HIVE:{hive_code}", hive_db_id, {
        "level": "Hive registration", "hive_id": hive_code, "actor": user["name"],
        "event_type": "Hive registration", "event_date": datetime.utcnow().isoformat(),
        "description": "Hive registered in HoneyChain."
    }, level="Hive registration")
    connection.commit()
    created = row(connection, "SELECT * FROM hives WHERE id = ?", (hive_db_id,))
    connection.close()
    return hive_output(created)


@app.get("/hives")
def get_hives(
    user=Depends(current_user)
):

    connection = db()

    hive_rows = rows(
        connection,
        """
        SELECT *
        FROM hives
        ORDER BY id
        """
    )

    connection.close()

    return [
        hive_output(hive)
        for hive in hive_rows
    ]


# New frontend-compatible API
@app.get("/api/hives/")
def get_api_hives(
    user=Depends(current_user)
):

    return get_hives(user)


@app.get("/api/hives")
def get_api_hives_no_slash(
    user=Depends(current_user)
):

    return get_hives(user)


@app.get("/hives/search")
def search_hives(
    q: str,
    user=Depends(current_user)
):

    connection = db()

    search = f"%{q.strip()}%"

    hive_rows = rows(
        connection,
        """
        SELECT *
        FROM hives

        WHERE
            hive_code LIKE ?
            OR location LIKE ?
            OR species LIKE ?
            OR status LIKE ?
            OR colony_strength LIKE ?

        ORDER BY id
        """,
        (
            search,
            search,
            search,
            search,
            search
        )
    )

    connection.close()

    return [
        hive_output(hive)
        for hive in hive_rows
    ]


@app.get("/api/hives/search")
def api_search_hives(
    q: str,
    user=Depends(current_user)
):

    return search_hives(q, user)


@app.get("/hives/{hive_code}")
def get_hive(
    hive_code: str,
    user=Depends(current_user)
):

    connection = db()

    hive = row(
        connection,
        """
        SELECT *
        FROM hives
        WHERE hive_code = ?
        """,
        (hive_code,)
    )

    connection.close()

    if not hive:

        raise HTTPException(
            status_code=404,
            detail="Hive not found"
        )

    return hive_output(hive)


# ============================================================
# HIVE SENSOR HISTORY
# ============================================================

@app.get("/hives/{hive_code}/history")
def hive_history(
    hive_code: str,
    user=Depends(current_user)
):

    connection = db()

    hive = row(
        connection,
        """
        SELECT id
        FROM hives
        WHERE hive_code = ?
        """,
        (hive_code,)
    )

    if not hive:

        connection.close()

        raise HTTPException(
            status_code=404,
            detail="Hive not found"
        )

    history = rows(
        connection,
        """
        SELECT
            day,
            temperature,
            humidity,
            weight
        FROM sensor_history

        WHERE hive_id = ?

        ORDER BY id
        """,
        (hive["id"],)
    )

    connection.close()

    return history


@app.get("/api/hives/{hive_code}/readings")
def api_hive_readings(
    hive_code: str,
    user=Depends(current_user)
):

    return hive_history(hive_code, user)


# ============================================================
# HIVE HEALTH
# ============================================================

@app.get("/health-score/{hive_code}")
def health_score(hive_code: str, user=Depends(current_user)):
    connection = db()
    hive = row(connection, "SELECT * FROM hives WHERE hive_code=?", (hive_code,))
    if not hive:
        connection.close()
        raise HTTPException(status_code=404, detail="Hive not found")
    readings = rows(
        connection,
        "SELECT timestamp,temperature,humidity FROM iot_sensor_readings "
        "WHERE hive_id=? ORDER BY timestamp DESC LIMIT 288",
        (hive["id"],)
    )
    # Use stored historical sensor records when the live IoT device has not
    # produced readings yet. This is still data-driven; no health score is
    # fabricated from the hive defaults.
    if not readings:
        readings = rows(
            connection,
            "SELECT day AS timestamp,temperature,humidity,weight FROM sensor_history "
            "WHERE hive_id=? ORDER BY id DESC LIMIT 288",
            (hive["id"],)
        )
        readings = list(reversed(readings))
    latest_ai = row(
        connection,
        "SELECT bee_health_score,bee_status,varroa_probability,created_at "
        "FROM health_analysis_results WHERE hive_id=? ORDER BY id DESC LIMIT 1",
        (hive["id"],)
    )
    if latest_ai:
        latest_ai = {
            "prediction": latest_ai.get("bee_status"),
            "visualScore": latest_ai.get("bee_health_score"),
            "varroaProbability": latest_ai.get("varroa_probability"),
            "created_at": latest_ai.get("created_at")
        }
    connection.close()

    result = analyze_hive(hive, list(reversed(readings)), latest_ai)
    return {"hiveId": hive_code, **result}


@app.get("/api/hives/{hive_code}/health")
def api_hive_health(hive_code: str, user=Depends(current_user)):
    return health_score(hive_code, user)


@app.post("/health-score/analyze")
async def analyze_hive_inputs(
    hive_id: str = Form(...),
    temperature: Optional[float] = Form(None),
    humidity: Optional[float] = Form(None),
    weight: Optional[float] = Form(None),
    image: Optional[UploadFile] = File(None),
    user=Depends(current_user),
):
    """Run Hive Health from supplied sensor values/history plus optional trained bee-image models."""
    connection = db()
    hive = row(connection, "SELECT * FROM hives WHERE hive_code=?", (hive_id,))
    if not hive:
        connection.close()
        raise HTTPException(status_code=404, detail="Hive not found")

    # Prefer explicitly supplied sensor values; otherwise use the latest stored IoT history.
    stored = rows(connection, "SELECT timestamp,temperature,humidity FROM iot_sensor_readings WHERE hive_id=? ORDER BY timestamp DESC LIMIT 288", (hive["id"],))
    readings = list(reversed(stored))
    if temperature is not None or humidity is not None:
        latest = readings[-1].copy() if readings else {"timestamp": datetime.utcnow().isoformat(), "temperature": hive.get("temperature", 33.0), "humidity": hive.get("humidity", 60.0)}
        if latest.get("temperature") is None: latest["temperature"] = hive.get("temperature", 33.0) or 33.0
        if latest.get("humidity") is None: latest["humidity"] = hive.get("humidity", 60.0) or 60.0
        if temperature is not None: latest["temperature"] = temperature
        if humidity is not None: latest["humidity"] = humidity
        readings = [latest]
        connection.execute("UPDATE hives SET temperature=COALESCE(?,temperature), humidity=COALESCE(?,humidity), weight=COALESCE(?,weight) WHERE id=?", (temperature, humidity, weight, hive["id"]))
    elif weight is not None:
        connection.execute("UPDATE hives SET weight=? WHERE id=?", (weight, hive["id"]))

    latest_ai = row(connection, "SELECT confidence,prediction,created_at FROM ai_predictions WHERE hive_id=? ORDER BY id DESC LIMIT 1", (hive["id"],))
    visual = None
    if image is not None:
        raw = await image.read()
        try:
            visual = predict_image(raw)
        except Exception as exc:
            connection.close()
            raise HTTPException(status_code=500, detail=f"Model inference failed: {exc}")
        prediction = visual["visualStatus"]
        explanation = f"Bee image screening found {visual['beeHealth']['class']} with {visual['beeHealth']['confidence']:.0%} class confidence; Varroa evidence is {visual['varroa']['infestedProbability']:.0%}."
        actions = ["Inspect the colony if the visual result is Monitor or High Concern.", "Use a clearer close-up image for follow-up screening.", "Treat model output as screening evidence, not a laboratory diagnosis."]
        connection.execute("INSERT INTO ai_predictions (hive_id,model_name,model_version,prediction,confidence,risk_level,severity,explanation,recommendations) VALUES (?,?,?,?,?,?,?,?,?)", (hive["id"],"HoneyChain Visual Fusion",visual["modelVersion"],prediction,visual["visualScore"],prediction,prediction,explanation,json.dumps(actions)))
        latest_ai = {"confidence":visual["visualScore"],"prediction":prediction,"created_at":datetime.utcnow().isoformat()}

    effective_hive = dict(hive)
    if weight is not None: effective_hive["weight"] = weight
    result = analyze_hive(effective_hive, readings, latest_ai)
    connection.commit(); connection.close()
    result["hiveId"] = hive_id
    result["visualEvidence"] = visual
    result["inputMode"] = "manual sensor input" if (temperature is not None or humidity is not None or weight is not None) else "stored IoT history"
    return result


# ============================================================
# PRODUCTION INTELLIGENCE
# ============================================================

class ProductionPredictionInput(BaseModel):
    hive_id: str
    environmental_temperature: float
    relative_humidity: float
    hive_temperature: float
    hive_humidity: float
    wind_speed: float
    total_hive_weight: float
    prediction_date: Optional[str] = None


def _production_context(connection, hive_id, prediction):
    history = rows(connection, "SELECT period, production FROM production_history WHERE hive_id=? ORDER BY id", (hive_id,))
    actuals = [float(x["production"]) for x in history if x.get("production") is not None]
    average = round(sum(actuals) / len(actuals), 2) if actuals else None
    if average is None:
        return None, None, "No historical production baseline is available yet.", [
            "Record actual harvest quantities for this hive so future predictions can be compared with its own history.",
            "Continue monitoring hive temperature, humidity, weight and forage conditions."
        ]
    change = round(((prediction - average) / average) * 100, 1) if average else 0.0
    return average, change, None, None


def _production_reasons(inp: ProductionPredictionInput, average, prediction):
    reasons = []
    actions = []
    if average is not None and prediction < average:
        if inp.total_hive_weight < 35:
            reasons.append({"title":"Lower hive weight", "detail":"The supplied total hive weight is relatively low compared with a stronger production state."})
            actions.append("Inspect colony strength, food stores and recent weight trend.")
        if inp.hive_temperature < 30 or inp.hive_temperature > 37:
            reasons.append({"title":"Hive temperature variation", "detail":"The supplied hive temperature is outside the normal operating band used for management review."})
            actions.append("Check hive placement, ventilation and colony thermal regulation.")
        if inp.hive_humidity < 40 or inp.hive_humidity > 80:
            reasons.append({"title":"Hive humidity variation", "detail":"The supplied hive humidity may indicate a condition worth checking."})
            actions.append("Check ventilation, moisture ingress and hive internal conditions.")
        if inp.relative_humidity > 85:
            reasons.append({"title":"High ambient humidity", "detail":"High external humidity can affect hive conditions and forage/flight activity."})
            actions.append("Monitor ventilation and local weather/forage conditions.")
        if inp.wind_speed > 25:
            reasons.append({"title":"Higher wind conditions", "detail":"Strong wind can reduce bee flight activity and foraging opportunities."})
            actions.append("Review forage access and colony activity after the windy period.")
        if not reasons:
            reasons.append({"title":"Combined environmental and hive factors", "detail":"The model prediction is below the hive's historical average, but the supplied inputs do not isolate one dominant cause."})
            actions.append("Review recent sensor trends, colony strength, forage availability and pest/disease screening together.")
        actions.append("Continue recording actual harvest quantities to improve hive-specific comparison.")
    else:
        actions = [
            "Continue regular hive inspections and sensor monitoring.",
            "Record actual harvest quantities consistently for future comparison."
        ]
    return reasons, actions


@app.post("/production/predict")
@app.post("/api/production/predict")
def predict_production(inp: ProductionPredictionInput, user=Depends(current_user)):
    connection = db()
    hive = row(connection, "SELECT * FROM hives WHERE hive_code=?", (inp.hive_id,))
    if not hive:
        connection.close()
        raise HTTPException(status_code=404, detail="Hive not found")
    try:
        pred_date = datetime.fromisoformat(inp.prediction_date).date() if inp.prediction_date else datetime.utcnow().date()
    except ValueError:
        connection.close()
        raise HTTPException(status_code=422, detail="prediction_date must be YYYY-MM-DD")
    try:
        prediction = predict_honey_production(
            inp.environmental_temperature, inp.relative_humidity, inp.hive_temperature,
            inp.hive_humidity, inp.wind_speed, inp.total_hive_weight, pred_date
        )
    except Exception as exc:
        connection.close()
        raise HTTPException(status_code=500, detail=f"Production model inference failed: {exc}")

    average, change, _, _ = _production_context(connection, hive["id"], prediction)
    reasons, actions = _production_reasons(inp, average, prediction)
    risk = "Low production risk" if average is not None and prediction < average * 0.90 else "Within available baseline"
    if average is None:
        risk = "Baseline unavailable"
    explanation = (f"Predicted production is {prediction:.2f} kg, {abs(change):.1f}% {'below' if change < 0 else 'above'} the hive's historical average." if average is not None else "Prediction generated from the trained production model; a hive-specific historical baseline is not available yet.")
    connection.execute("""INSERT INTO production_predictions
        (hive_id,prediction,prediction_date,environmental_temperature,relative_humidity,hive_temperature,hive_humidity,wind_speed,total_hive_weight,historical_average,change_percent,risk_level,reasons,recommended_actions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (hive["id"],prediction,pred_date.isoformat(),inp.environmental_temperature,inp.relative_humidity,inp.hive_temperature,inp.hive_humidity,inp.wind_speed,inp.total_hive_weight,average,change,risk,json.dumps(reasons),json.dumps(actions)))
    connection.commit()
    prediction_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    connection.close()
    return {
        "id": prediction_id, "hiveId": inp.hive_id, "predictedProduction": round(prediction,2),
        "historicalAverage": average, "changePercent": change, "riskLevel": risk,
        "explanation": explanation, "reasons": reasons, "recommendedActions": actions,
        "features": {k:v for k,v in zip(production_model_info()["features"], [inp.environmental_temperature, inp.relative_humidity, inp.hive_temperature, inp.hive_humidity, inp.wind_speed, inp.total_hive_weight, pred_date.month, pred_date.timetuple().tm_yday])},
        "model": production_model_info()
    }


@app.get("/production/{hive_code}")
@app.get("/api/hives/{hive_code}/production")
def production(hive_code: str, user=Depends(current_user)):
    connection = db()
    hive = row(connection, "SELECT * FROM hives WHERE hive_code=?", (hive_code,))
    if not hive:
        connection.close(); raise HTTPException(status_code=404, detail="Hive not found")
    latest = row(connection, "SELECT * FROM production_predictions WHERE hive_id=? ORDER BY id DESC LIMIT 1", (hive["id"],))
    history = rows(connection, "SELECT period,production FROM production_history WHERE hive_id=? ORDER BY id", (hive["id"],))
    predictions = rows(connection, "SELECT prediction_date,prediction,historical_average,change_percent,risk_level FROM production_predictions WHERE hive_id=? ORDER BY id DESC LIMIT 30", (hive["id"],))
    connection.close()
    result = {"hiveId":hive_code,"history":history,"predictions":predictions,"model":production_model_info()}
    if latest:
        result.update({"predictedProduction":latest["prediction"],"historicalAverage":latest["historical_average"],"changePercent":latest["change_percent"],"riskLevel":latest["risk_level"],"reasons":json.loads(latest["reasons"] or "[]"),"recommendedActions":json.loads(latest["recommended_actions"] or "[]"),"predictionDate":latest["prediction_date"]})
    else:
        result.update({"predictedProduction":None,"historicalAverage":None,"changePercent":None,"riskLevel":"No prediction yet","reasons":[],"recommendedActions":[]})
    return result


@app.get("/api/admin/production-alerts")
def admin_production_alerts(user=Depends(require("admin"))):
    connection = db()
    data = rows(connection, """SELECT p.*, h.hive_code, h.location FROM production_predictions p JOIN hives h ON h.id=p.hive_id ORDER BY p.id DESC LIMIT 100""")
    connection.close()
    alerts=[]
    for x in data:
        if x.get("risk_level") == "Low production risk":
            alerts.append({"hiveId":x["hive_code"],"location":x["location"],"predictedProduction":x["prediction"],"historicalAverage":x["historical_average"],"changePercent":x["change_percent"],"riskLevel":x["risk_level"],"reasons":json.loads(x["reasons"] or "[]"),"recommendedActions":json.loads(x["recommended_actions"] or "[]"),"predictionDate":x["prediction_date"]})
    return alerts

# ============================================================
# SENSOR DATA
# ============================================================

class SensorReadingInput(BaseModel):
    hive_id: str
    temperature: float
    humidity: float
    weight: Optional[float] = None
    battery_level: Optional[float] = None
    signal_strength: Optional[float] = None


class IoTReadingInput(BaseModel):
    hive_id: str
    timestamp: Optional[str] = None
    temperature: float
    humidity: float


def _check_iot_key(value: Optional[str]):
    configured = os.getenv("HONEYCHAIN_IOT_KEY", "")
    if configured and value != configured:
        raise HTTPException(status_code=401, detail="Invalid IoT device key")


@app.post("/api/iot/readings")
@app.post("/iot/readings")
def create_iot_reading(reading: IoTReadingInput, x_device_key: Optional[str] = Header(None, alias="X-Device-Key")):
    _check_iot_key(x_device_key)
    connection = db()
    hive = row(connection, "SELECT id, hive_code FROM hives WHERE hive_code = ?", (reading.hive_id,))
    if not hive:
        connection.close()
        raise HTTPException(status_code=404, detail="Hive not found")

    timestamp = reading.timestamp or datetime.utcnow().isoformat() + "Z"
    connection.execute(
        "INSERT INTO iot_sensor_readings (hive_id,timestamp,temperature,humidity) VALUES (?,?,?,?)",
        (hive["id"], timestamp, reading.temperature, reading.humidity)
    )
    connection.execute(
        "UPDATE hives SET temperature=?, humidity=? WHERE id=?",
        (reading.temperature, reading.humidity, hive["id"])
    )
    reading_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    add_blockchain_block(connection, f"HIVE:{reading.hive_id}", reading_id, {
        "level":"IoT reading", "hive_id":reading.hive_id, "event_type":"IoT reading",
        "event_date":timestamp, "temperature":reading.temperature, "humidity":reading.humidity,
        "description":"IoT device push reading recorded."
    }, level="IoT reading")
    connection.commit(); connection.close()
    return {"ok": True, "hiveId": reading.hive_id, "timestamp": timestamp}



@app.post("/api/iot/csv/{hive_code}")
async def import_iot_csv(hive_code: str, file: UploadFile = File(...), user=Depends(current_user)):
    if user["role"] not in ("beekeeper", "admin"):
        raise HTTPException(status_code=403, detail="IoT import requires beekeeper or admin access")
    connection = db()
    hive = row(connection, "SELECT id FROM hives WHERE hive_code=?", (hive_code,))
    if not hive:
        connection.close(); raise HTTPException(status_code=404, detail="Hive not found")
    raw = await file.read()
    import csv as _csv
    try:
        text = raw.decode("utf-8-sig")
        reader = _csv.DictReader(io.StringIO(text))
        imported = 0
        for item in reader:
            low = {str(k).strip().lower(): v for k,v in item.items()}
            t = next((low[k] for k in ("temperature","temp","temperature_c","temp_c") if k in low and low[k] not in (None,"")), None)
            h = next((low[k] for k in ("humidity","hum","humidity_pct","rh") if k in low and low[k] not in (None,"")), None)
            ts = next((low[k] for k in ("timestamp","time","datetime","date") if k in low and low[k] not in (None,"")), None)
            if t is None or h is None: continue
            timestamp = str(ts) if ts else datetime.utcnow().isoformat()+"Z"
            connection.execute("INSERT INTO iot_sensor_readings (hive_id,timestamp,temperature,humidity) VALUES (?,?,?,?)", (hive["id"],timestamp,float(t),float(h)))
            imported += 1
        if imported:
            latest = connection.execute("SELECT timestamp,temperature,humidity FROM iot_sensor_readings WHERE hive_id=? ORDER BY timestamp DESC LIMIT 1", (hive["id"],)).fetchone()
            connection.execute("UPDATE hives SET temperature=?,humidity=? WHERE id=?", (latest["temperature"],latest["humidity"],hive["id"]))
            add_blockchain_block(connection, f"HIVE:{hive_code}", None, {"level":"IoT CSV import","hive_id":hive_code,"event_type":"IoT CSV import","event_date":datetime.utcnow().isoformat(),"rows":imported,"filename":file.filename,"description":"IoT sensor CSV readings imported."}, level="IoT CSV import")
        connection.commit()
        return {"ok":True,"hiveId":hive_code,"imported":imported,"filename":file.filename}
    except Exception as exc:
        connection.rollback(); raise HTTPException(status_code=422, detail=f"CSV import failed: {exc}")
    finally:
        connection.close()

@app.get("/api/iot/readings/{hive_code}")
@app.get("/iot/readings/{hive_code}")
def get_iot_readings(hive_code: str, limit: int = 200, user=Depends(current_user)):
    connection = db()
    hive = row(connection, "SELECT id FROM hives WHERE hive_code=?", (hive_code,))
    if not hive:
        connection.close(); raise HTTPException(status_code=404, detail="Hive not found")
    limit = max(1, min(limit, 1000))
    data = rows(connection, f"SELECT timestamp,temperature,humidity,received_at FROM iot_sensor_readings WHERE hive_id=? ORDER BY timestamp DESC LIMIT {limit}", (hive["id"],))
    connection.close()
    return list(reversed(data))


@app.get("/api/iot/latest/{hive_code}")
@app.get("/iot/latest/{hive_code}")
def get_iot_latest(hive_code: str, user=Depends(current_user)):
    connection = db()
    data = row(connection, """SELECT r.timestamp,r.temperature,r.humidity,r.received_at,h.hive_code
                              FROM iot_sensor_readings r JOIN hives h ON h.id=r.hive_id
                              WHERE h.hive_code=? ORDER BY r.timestamp DESC LIMIT 1""", (hive_code,))
    connection.close()
    if not data: return {"connected": False, "hiveId": hive_code}
    return {"connected": True, **data}


def _parse_sensor_payload(payload):
    """Accept common ESP32 JSON/text shapes without requiring a fixed firmware schema."""
    def walk(obj):
        if isinstance(obj, dict):
            low = {str(k).lower(): v for k, v in obj.items()}
            t = next((low[k] for k in ('temperature','temp','temperature_c','temp_c') if k in low), None)
            h = next((low[k] for k in ('humidity','hum','humidity_pct','rh') if k in low), None)
            ts = next((low[k] for k in ('timestamp','time','datetime','ts') if k in low), None)
            if t is not None and h is not None:
                return t, h, ts
            for v in obj.values():
                found = walk(v)
                if found: return found
        elif isinstance(obj, list):
            for v in obj:
                found = walk(v)
                if found: return found
        return None
    found = walk(payload)
    if found: return found
    if isinstance(payload, str):
        tm = re.search(r'(?:temperature|temp)\s*[:=]\s*(-?\d+(?:\.\d+)?)', payload, re.I)
        hm = re.search(r'(?:humidity|hum|rh)\s*[:=]\s*(-?\d+(?:\.\d+)?)', payload, re.I)
        if tm and hm: return float(tm.group(1)), float(hm.group(1)), None
    raise ValueError('Sensor response does not contain temperature and humidity')


def fetch_iot_device():
    urls = [IOT_DEVICE_URL.rstrip('/') + p for p in ('/', '/data', '/sensor', '/readings', '/api/data')]
    last_error = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'Accept':'application/json,text/plain,*/*','User-Agent':'HoneyChain-IoT/1.0'})
            with urllib.request.urlopen(req, timeout=4) as response:
                raw = response.read().decode('utf-8', errors='replace')
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = raw
            t, h, ts = _parse_sensor_payload(payload)
            t, h = float(t), float(h)
            if not (-50 <= t <= 80 and 0 <= h <= 100):
                raise ValueError('Sensor values are outside valid ranges')
            return {'temperature': t, 'humidity': h, 'timestamp': str(ts) if ts else datetime.utcnow().isoformat() + 'Z', 'sourceUrl': url}
        except Exception as exc:
            last_error = exc
    raise HTTPException(status_code=502, detail=f'Unable to read IoT sensor at {IOT_DEVICE_URL}: {last_error}')


@app.get('/api/iot/config')
@app.get('/iot/config')
def iot_config(user=Depends(current_user)):
    return {
        'mode': 'pull' if IOT_PULL_ENABLED else 'push',
        'pullEnabled': IOT_PULL_ENABLED,
        'deviceUrl': IOT_DEVICE_URL if IOT_PULL_ENABLED else None,
        'intervalSeconds': 15,
        'pushEndpoint': '/api/iot/readings'
    }

@app.get('/api/iot/sync/{hive_code}')
@app.get('/iot/sync/{hive_code}')
def sync_iot_reading(hive_code: str, user=Depends(current_user)):
    if not IOT_PULL_ENABLED:
        raise HTTPException(status_code=409, detail='IoT is configured for push mode. The device should POST every 15 seconds to /api/iot/readings.')
    connection = db()
    hive = row(connection, 'SELECT id, hive_code FROM hives WHERE hive_code=?', (hive_code,))
    if not hive:
        connection.close(); raise HTTPException(status_code=404, detail='Hive not found')
    connection.close()
    reading = fetch_iot_device()
    connection = db()
    connection.execute('INSERT INTO iot_sensor_readings (hive_id,timestamp,temperature,humidity) VALUES (?,?,?,?)',
                       (hive['id'], reading['timestamp'], reading['temperature'], reading['humidity']))
    connection.execute('UPDATE hives SET temperature=?, humidity=? WHERE id=?',
                       (reading['temperature'], reading['humidity'], hive['id']))
    sensor_event_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    add_blockchain_block(connection, f"HIVE:{hive_code}", sensor_event_id, {
        "level": "IoT reading", "hive_id": hive_code, "event_type": "IoT reading",
        "event_date": reading["timestamp"], "temperature": reading["temperature"], "humidity": reading["humidity"],
        "description": "Live Wi-Fi sensor reading recorded."
    }, level="IoT reading")
    connection.commit(); connection.close()
    return {'connected': True, 'hiveId': hive_code, **reading}


def sensor_condition(readings):
    if not readings:
        return {"score": None, "status": "No IoT data", "evidence": None, "sampleCount": 0}
    t = readings[-1]["temperature"]; h = readings[-1]["humidity"]
    # Transparent condition engine; it becomes more useful as real readings accumulate.
    temp_score = max(0.0, 1.0 - abs(t - 33.0) / 8.0)
    hum_score = max(0.0, 1.0 - abs(h - 60.0) / 40.0)
    score = round((0.55*temp_score + 0.45*hum_score)*100, 1)
    status = "Healthy" if score >= 75 else "Monitor" if score >= 55 else "Needs Attention"
    return {"score": score, "status": status, "evidence": round(1-score/100,4), "sampleCount": len(readings), "temperature": t, "humidity": h}


@app.get("/api/iot/health/{hive_code}")
@app.get("/iot/health/{hive_code}")
def get_iot_health(hive_code: str, user=Depends(current_user)):
    readings = get_iot_readings(hive_code, 200, user)
    return {"hiveId": hive_code, **sensor_condition(readings)}


# Backward-compatible authenticated sensor endpoint used by earlier ESP32 integrations.
@app.post("/api/sensors/readings")
def create_sensor_reading(reading: SensorReadingInput, user=Depends(current_user)):
    connection = db()
    hive = row(connection, "SELECT id FROM hives WHERE hive_code=?", (reading.hive_id,))
    if not hive:
        connection.close(); raise HTTPException(status_code=404, detail="Hive not found")
    connection.execute("INSERT INTO sensor_readings (hive_id,temperature,humidity,weight,battery_level,signal_strength) VALUES (?,?,?,?,?,?)",
                       (hive["id"],reading.temperature,reading.humidity,reading.weight,reading.battery_level,reading.signal_strength))
    connection.execute("UPDATE hives SET temperature=?,humidity=?,weight=COALESCE(?,weight) WHERE id=?",
                       (reading.temperature,reading.humidity,reading.weight,hive["id"]))
    connection.commit(); connection.close()
    return {"ok":True,"message":"Sensor reading stored","hiveId":reading.hive_id}


# ============================================================
# BEE / HIVE HEALTH ANALYSIS
# ============================================================

def _risk_band(probability):
    p=float(probability or 0)
    if p >= 0.75: return "High"
    if p >= 0.50: return "Medium"
    return "Low"


def _save_health_result(connection, hive_id, image_name, visual, hive_result):
    bee_score = round(max(0.0, min(100.0, (1.0 - float(visual.get("visualScore", 0))) * 100.0)), 1)
    varroa_prob = float((visual.get("varroa") or {}).get("infestedProbability", 0))
    bee_status = "Healthy" if bee_score >= 75 else "Monitor" if bee_score >= 50 else "Needs Attention"
    connection.execute("""INSERT INTO health_analysis_results
        (hive_id,image_name,bee_status,bee_health_score,bee_class,varroa_risk,varroa_probability,mite_count,
         sensor_score,hive_score,hive_status,temperature,humidity,weight,visual_model)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (hive_id,image_name,bee_status,bee_score,
         (visual.get("beeHealth") or {}).get("class"),_risk_band(varroa_prob),varroa_prob,
         int((visual.get("varroa") or {}).get("miteCount",0)),hive_result.get("sensorScore"),hive_result.get("score"),
         hive_result.get("status"),hive_result.get("temperature"),hive_result.get("humidity"),hive_result.get("weight"),
         visual.get("modelVersion")))
    return bee_score, bee_status, _risk_band(varroa_prob)


@app.get("/bee-health/results")
def bee_health_results(user=Depends(current_user), hive_id: Optional[str] = None):
    connection=db()
    sql="""SELECT r.*, h.hive_code FROM health_analysis_results r
           JOIN hives h ON h.id=r.hive_id"""
    params=[]
    if hive_id:
        sql += " WHERE h.hive_code=?"
        params.append(hive_id)
    sql += " ORDER BY r.id DESC LIMIT 200"
    data=rows(connection,sql,tuple(params)); connection.close()
    return [{
        "id":r["id"],"hiveId":r["hive_code"],"imageName":r["image_name"],"beeStatus":r["bee_status"],
        "beeHealthScore":r["bee_health_score"],"beeClass":r["bee_class"],"varroaRisk":r["varroa_risk"],
        "varroaProbability":r["varroa_probability"],"miteCount":r["mite_count"],"sensorScore":r["sensor_score"],
        "hiveScore":r["hive_score"],"hiveStatus":r["hive_status"],"temperature":r["temperature"],
        "humidity":r["humidity"],"weight":r["weight"],"visualModel":r["visual_model"],"createdAt":r["created_at"]
    } for r in data]


@app.post("/bee-health/analyze")
async def bee_health_analyze(
    hive_id: str = Form(...),
    image: UploadFile = File(...),
    temperature: Optional[float] = Form(None),
    humidity: Optional[float] = Form(None),
    weight: Optional[float] = Form(None),
    user=Depends(current_user),
):
    connection=db()
    hive=row(connection,"SELECT * FROM hives WHERE hive_code=?",(hive_id,))
    if not hive:
        connection.close(); raise HTTPException(status_code=404,detail="Hive not found")
    raw=await image.read()
    try:
        visual=predict_image(raw)
    except Exception as exc:
        connection.close(); raise HTTPException(status_code=500,detail=f"Model inference failed: {exc}")

    stored=rows(connection,"SELECT timestamp,temperature,humidity FROM iot_sensor_readings WHERE hive_id=? ORDER BY timestamp DESC LIMIT 288",(hive["id"],))
    readings=list(reversed(stored))
    if temperature is not None or humidity is not None:
        latest=readings[-1].copy() if readings else {"timestamp":datetime.utcnow().isoformat(),"temperature":hive.get("temperature"),"humidity":hive.get("humidity")}
        if temperature is not None: latest["temperature"]=temperature
        if humidity is not None: latest["humidity"]=humidity
        readings=[latest]
    elif not readings:
        # Do not invent a health signal; use no sensor evidence when live history is unavailable.
        readings=[]
    effective=dict(hive)
    if weight is not None: effective["weight"]=weight
    visual_risk=visual.get("visualStatus")
    ai_record={"prediction":visual_risk,"confidence":visual.get("visualScore"),"created_at":datetime.utcnow().isoformat()}
    hive_result=analyze_hive(effective,readings,ai_record)
    bee_score,bee_status,varroa_risk=_save_health_result(connection,hive["id"],image.filename,visual,hive_result)
    analysis_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    linked_batches = rows(connection,"SELECT batch_id FROM batches WHERE hive_id=? AND status IN ('Harvested','Health Assessed','Ready for Processing')",(hive["id"],))
    for linked in linked_batches:
        if not connection.execute("SELECT 1 FROM traceability_events WHERE batch_id=? AND event_type='Health assessed' LIMIT 1",(linked["batch_id"],)).fetchone():
            add_trace_event(connection,linked["batch_id"],"Health assessed",user["name"],user["id"],"Bee and hive health assessment recorded for the source hive.",{"hive_id":hive_id,"bee_health_score":bee_score,"hive_score":hive_result.get("score"),"temperature":hive_result.get("temperature"),"humidity":hive_result.get("humidity")})
            connection.execute("UPDATE batches SET status='Health Assessed' WHERE batch_id=?",(linked["batch_id"],))
    add_blockchain_block(connection, f"HIVE:{hive_id}", analysis_id, {
        "level": "Health analysis", "hive_id": hive_id, "event_type": "Health analysis",
        "event_date": datetime.utcnow().isoformat(), "bee_health_score": bee_score,
        "hive_score": hive_result.get("score"), "temperature": hive_result.get("temperature"),
        "humidity": hive_result.get("humidity"), "model": hive_result.get("model"),
        "description": "Bee and hive health evidence analysis recorded."
    }, level="Health analysis")
    connection.execute("UPDATE hives SET temperature=COALESCE(?,temperature), humidity=COALESCE(?,humidity), weight=COALESCE(?,weight) WHERE id=?",(temperature,humidity,weight,hive["id"]))
    connection.commit()
    saved=row(connection,"SELECT created_at FROM health_analysis_results WHERE id=last_insert_rowid()")
    connection.close()
    return {
        "hiveId":hive_id,"beeHealthScore":bee_score,"beeStatus":bee_status,"beeClass":(visual.get("beeHealth") or {}).get("class"),
        "varroaRisk":varroa_risk,"hiveScore":hive_result.get("score"),"hiveStatus":hive_result.get("status"),
        "sensorScore":hive_result.get("sensorScore"),"temperature":hive_result.get("temperature"),"humidity":hive_result.get("humidity"),
        "weight":hive_result.get("weight"),"visual":visual,"sensorModel":hive_result.get("sensorModel"),
        "createdAt":saved["created_at"] if saved else datetime.utcnow().isoformat(),"model":hive_result.get("model")
    }


# ============================================================
# AI IMAGE ANALYSIS
# ============================================================

@app.post("/ai/analyze")
async def ai_analyze(image: UploadFile = File(...), hive_id: Optional[str] = Form(None), user=Depends(current_user)):
    raw = await image.read()
    try:
        result = predict_image(raw)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Model inference failed: {exc}")

    connection = db(); hive_db_id = None
    if hive_id:
        hive = row(connection, "SELECT id FROM hives WHERE hive_code=?", (hive_id,))
        if hive: hive_db_id = hive["id"]

    prediction = result["visualStatus"]
    explanation = f"Bee image screening found {result['beeHealth']['class']} with {result['beeHealth']['confidence']:.0%} class confidence; Varroa evidence is {result['varroa']['infestedProbability']:.0%}."
    actions = ["Inspect the colony if the visual result is Monitor or High Concern.", "Use a clearer close-up image for follow-up screening.", "Treat model output as screening evidence, not a laboratory diagnosis."]
    connection.execute("""INSERT INTO ai_predictions (hive_id,model_name,model_version,prediction,confidence,risk_level,severity,explanation,recommendations)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                       (hive_db_id,"HoneyChain Visual Fusion",result["modelVersion"],prediction,result["visualScore"],prediction,prediction,explanation,json.dumps(actions)))
    connection.commit(); connection.close()
    return {"filename": image.filename, "finding": prediction, "confidence": result["visualScore"], "risk": prediction,
            "explanation": explanation, "recommendedActions": actions, "visual": result,
            "disclaimer": "AI image screening is not a definitive diagnosis."}




def workflow_status(connection, batch_id):
    batch = row(connection, "SELECT * FROM batches WHERE batch_id=?", (batch_id,))
    if not batch:
        return None
    event_types = {x["event_type"] for x in rows(connection, "SELECT event_type FROM traceability_events WHERE batch_id=?", (batch_id,))}
    return {"batchId":batch_id,"status":batch.get("status") or "Harvested","steps":[
        {"key":"hive","label":"Hive source","complete":"Hive source" in event_types},
        {"key":"harvest","label":"Harvest recorded","complete":"Harvest recorded" in event_types},
        {"key":"health","label":"Health assessed","complete":"Health assessed" in event_types},
        {"key":"processing","label":"Processing verified","complete":"Processing verified" in event_types},
        {"key":"packaging","label":"Packaging verified","complete":"Packaging verified" in event_types},
        {"key":"admin","label":"Admin validation","complete":"Admin validation" in event_types},
        {"key":"qr","label":"QR issued","complete":"QR issued" in event_types},
    ]}

def add_trace_event(connection, batch_id, event_type, actor, actor_id, description, metadata=None):
    previous = connection.execute("SELECT event_hash FROM traceability_events WHERE batch_id=? ORDER BY id DESC LIMIT 1", (batch_id,)).fetchone()
    previous_hash = previous["event_hash"] if previous else None
    payload = {"batch_id":batch_id,"event_type":event_type,"actor":actor,"event_date":datetime.utcnow().date().isoformat(),"description":description,"metadata":metadata or {}}
    event_hash = create_event_hash(payload, previous_hash)
    connection.execute("INSERT INTO traceability_events (batch_id,event_type,actor,actor_id,event_date,description,metadata,previous_hash,event_hash) VALUES (?,?,?,?,?,?,?,?,?)", (batch_id,event_type,actor,actor_id,payload["event_date"],description,json.dumps(metadata or {},sort_keys=True),previous_hash,event_hash))
    event_id=connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    add_blockchain_block(connection,batch_id,event_id,payload,level=event_type)
    return event_id

def require_batch_state(connection, batch_id, allowed):
    batch=row(connection,"SELECT * FROM batches WHERE batch_id=?",(batch_id,))
    if not batch: raise HTTPException(status_code=404,detail="Batch not found")
    if batch.get("status") not in allowed:
        raise HTTPException(status_code=409,detail=f"Batch is currently '{batch.get('status')}'. Required state: {', '.join(allowed)}")
    return batch

# ============================================================
# BATCHES
# ============================================================

@app.get("/batches")
def get_batches(
    user=Depends(current_user)
):

    connection = db()

    base = """SELECT b.*, h.hive_code, h.location FROM batches b LEFT JOIN hives h ON b.hive_id=h.id"""
    params=()
    if user["role"] == "beekeeper":
        query=base+" WHERE h.beekeeper_id=? ORDER BY b.created_at DESC"; params=(user["id"],)
    elif user["role"] == "processor":
        query=base+" WHERE b.status IN ('Harvested','Health Assessed','Processed','Ready for Packaging','Packaged','Awaiting Admin Validation','Validated') ORDER BY b.created_at DESC"
    elif user["role"] == "packaging":
        query=base+" WHERE b.status IN ('Ready for Packaging','Packaged','Awaiting Admin Validation','Validated') ORDER BY b.created_at DESC"
    else:
        query=base+" ORDER BY b.created_at DESC"
    batch_rows = rows(connection, query, params)

    connection.close()

    return batch_rows


@app.get("/api/batches")
def api_get_batches(
    user=Depends(current_user)
):

    return get_batches(user)


@app.post("/batches")
def create_batch(
    batch: BatchCreate,
    user=Depends(current_user)
):

    if user["role"] != "beekeeper":

        raise HTTPException(
            status_code=403,
            detail="Only beekeepers can create harvest batches"
        )

    connection = db()

    hive = row(
        connection,
        """
        SELECT id
        FROM hives
        WHERE hive_code = ? AND beekeeper_id = ?
        """,
        (batch.hiveId, user["id"])
    )

    if not hive:

        connection.close()

        raise HTTPException(
            status_code=404,
            detail="Hive not found"
        )

    batch_id = (
        "HC-BATCH-"
        + secrets.token_hex(4).upper()
    )

    previous = connection.execute(
        """
        SELECT integrity_hash
        FROM batches
        ORDER BY created_at DESC, batch_id DESC
        LIMIT 1
        """
    ).fetchone()

    previous_hash = (
        previous["integrity_hash"]
        if previous
        else None
    )

    payload = {
        "batch_id": batch_id,
        "hive_id": batch.hiveId,
        "honey_type": batch.honeyType,
        "quantity": batch.quantity,
        "harvest_date": batch.harvestDate,
        "origin": batch.origin
    }

    integrity_hash = create_event_hash(
        payload,
        previous_hash
    )

    connection.execute(
        """
        INSERT INTO batches
        (
            batch_id,
            hive_id,
            honey_type,
            quantity,
            harvest_date,
            origin,
            status,
            created_at,
            integrity_hash,
            previous_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            hive["id"],
            batch.honeyType,
            batch.quantity,
            batch.harvestDate,
            batch.origin,
            "Harvested",
            datetime.utcnow().isoformat(),
            integrity_hash,
            previous_hash
        )
    )

    source_payload = {
        "batch_id": batch_id,
        "event_type": "Hive source",
        "actor": user["name"],
        "event_date": batch.harvestDate,
        "description": f"Batch linked to source hive {batch.hiveId} at {batch.origin}."
    }
    source_hash = create_event_hash(source_payload, None)
    connection.execute(
        """INSERT INTO traceability_events
        (batch_id,event_type,actor,actor_id,event_date,description,metadata,event_hash)
        VALUES (?,?,?,?,?,?,?,?)""",
        (batch_id, "Hive source", user["name"], user["id"], batch.harvestDate,
         source_payload["description"],
         json.dumps({"hive_id": batch.hiveId, "origin": batch.origin}), source_hash)
    )
    source_event_id = connection.execute(
        "SELECT last_insert_rowid() AS id"
    ).fetchone()["id"]
    add_blockchain_block(connection, batch_id, source_event_id, source_payload)

    event_payload = {
        "batch_id": batch_id,
        "event_type": "Harvest recorded",
        "actor": user["name"],
        "event_date": batch.harvestDate,
        "description": "Honey harvest batch created."
    }

    event_hash = create_event_hash(
        event_payload,
        source_hash
    )

    connection.execute(
        """
        INSERT INTO traceability_events
        (
            batch_id,
            event_type,
            actor,
            actor_id,
            event_date,
            description,
            metadata,
            event_hash
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch_id,
            "Harvest recorded",
            user["name"],
            user["id"],
            batch.harvestDate,
            "Honey harvest batch created.",
            "{}",
            event_hash
        )
    )
    harvest_event_id = connection.execute(
        "SELECT last_insert_rowid() AS id"
    ).fetchone()["id"]
    add_blockchain_block(connection, batch_id, harvest_event_id, event_payload)

    connection.commit()
    connection.close()

    return {
        "ok": True,
        "batchId": batch_id,
        "integrityHash": integrity_hash
    }


@app.post("/api/batches")
def api_create_batch(
    batch: BatchCreate,
    user=Depends(current_user)
):

    return create_batch(batch, user)


# ============================================================
# SINGLE BATCH
# ============================================================

@app.get("/batches/{batch_id}")
def get_batch(
    batch_id: str,
    user=Depends(current_user)
):

    connection = db()

    batch = row(
        connection,
        """
        SELECT
            b.*,
            h.hive_code,
            h.location

        FROM batches b

        LEFT JOIN hives h
            ON b.hive_id = h.id

        WHERE b.batch_id = ?
        """,
        (batch_id,)
    )

    if not batch:

        connection.close()

        raise HTTPException(
            status_code=404,
            detail="Batch not found"
        )

    events = rows(
        connection,
        """
        SELECT
            event_type,
            actor,
            event_date,
            description,
            event_hash,
            previous_hash

        FROM traceability_events

        WHERE batch_id = ?

        ORDER BY id
        """,
        (batch_id,)
    )

    connection.close()

    return {
        "batch": batch,
        "events": events
    }


@app.get("/api/batches/{batch_id}")
def api_get_batch(
    batch_id: str,
    user=Depends(current_user)
):

    return get_batch(batch_id, user)


# ============================================================
# TRACEABILITY
# ============================================================

@app.get("/batches/{batch_id}/timeline")
def batch_timeline(
    batch_id: str,
    user=Depends(current_user)
):

    connection = db()

    events = rows(
        connection,
        """
        SELECT
            event_type,
            actor,
            event_date,
            description,
            event_hash,
            previous_hash

        FROM traceability_events

        WHERE batch_id = ?

        ORDER BY id
        """,
        (batch_id,)
    )

    connection.close()

    return {
        "batchId": batch_id,
        "timeline": events
    }


@app.get("/api/batches/{batch_id}/timeline")
def api_batch_timeline(
    batch_id: str,
    user=Depends(current_user)
):

    return batch_timeline(batch_id, user)


# ============================================================
# INTEGRITY
# ============================================================

@app.get("/batches/{batch_id}/integrity")
def batch_integrity(
    batch_id: str,
    user=Depends(current_user)
):

    connection = db()

    batch = row(
        connection,
        """
        SELECT *
        FROM batches
        WHERE batch_id = ?
        """,
        (batch_id,)
    )

    events = rows(
        connection,
        """
        SELECT *
        FROM traceability_events
        WHERE batch_id = ?
        ORDER BY id
        """,
        (batch_id,)
    )

    connection.close()

    if not batch:

        raise HTTPException(
            status_code=404,
            detail="Batch not found"
        )

    chain_valid = True

    previous = None

    for event in events:

        if event["previous_hash"] != previous:

            chain_valid = False
            break

        previous = event["event_hash"]

    return {
        "batchId": batch_id,
        "integrityHash": batch["integrity_hash"],
        "eventCount": len(events),
        "chainValid": chain_valid,
        "integrityStatus": (
            "Verified"
            if chain_valid
            else "Integrity issue detected"
        )
    }


@app.get("/api/batches/{batch_id}/integrity")
def api_batch_integrity(
    batch_id: str,
    user=Depends(current_user)
):

    return batch_integrity(batch_id, user)


# ============================================================
# PROCESSING
# ============================================================

class ProcessingRecord(BaseModel):

    batch_id: str
    process_type: str = "Standard Processing"
    input_quantity: float
    output_quantity: float
    quality_notes: Optional[str] = None


@app.get("/batches/{batch_id}/processing")
def get_processing(
    batch_id: str,
    user=Depends(current_user)
):

    connection = db()

    events = rows(
        connection,
        """
        SELECT *
        FROM traceability_events

        WHERE
            batch_id = ?
            AND event_type = 'Processing'

        ORDER BY id
        """,
        (batch_id,)
    )

    connection.close()

    return events


@app.post("/processing")
def add_processing(record: ProcessingRecord, user=Depends(current_user)):
    if user["role"] != "processor": raise HTTPException(status_code=403,detail="Processor access required")
    connection=db(); require_batch_state(connection,record.batch_id,["Harvested","Health Assessed","Ready for Processing"])
    metadata={"process_type":record.process_type,"input_quantity":record.input_quantity,"output_quantity":record.output_quantity,"quality_notes":record.quality_notes}
    add_trace_event(connection,record.batch_id,"Processing",user["name"],user["id"],"Processing record submitted by the processor.",metadata)
    connection.execute("UPDATE batches SET status='Processed' WHERE batch_id=?",(record.batch_id,))
    connection.commit(); connection.close(); return {"ok":True,"batchId":record.batch_id,"status":"Processed"}

@app.post("/processing/{batch_id}/verify")
def verify_processing(batch_id:str,user=Depends(current_user)):
    if user["role"]!="processor": raise HTTPException(status_code=403,detail="Processor access required")
    connection=db(); require_batch_state(connection,batch_id,["Processed"])
    add_trace_event(connection,batch_id,"Processing verified",user["name"],user["id"],"Processor verified the completed processing record.")
    connection.execute("UPDATE batches SET status='Ready for Packaging' WHERE batch_id=?",(batch_id,))
    connection.commit(); connection.close(); return {"ok":True,"batchId":batch_id,"status":"Ready for Packaging"}


# ============================================================
# PACKAGING
# ============================================================

class PackagingRecord(BaseModel):

    batch_id: str
    package_size: str
    quantity: int
    lot_number: str


@app.get("/batches/{batch_id}/packaging")
def get_packaging(
    batch_id: str,
    user=Depends(current_user)
):

    connection = db()

    events = rows(
        connection,
        """
        SELECT *
        FROM traceability_events

        WHERE
            batch_id = ?
            AND event_type = 'Packaging'

        ORDER BY id
        """,
        (batch_id,)
    )

    connection.close()

    return events


@app.post("/packaging")
def add_packaging(record: PackagingRecord,user=Depends(current_user)):
    if user["role"]!="packaging": raise HTTPException(status_code=403,detail="Packaging unit access required")
    connection=db(); require_batch_state(connection,record.batch_id,["Ready for Packaging"])
    metadata={"package_size":record.package_size,"quantity":record.quantity,"lot_number":record.lot_number}
    add_trace_event(connection,record.batch_id,"Packaging",user["name"],user["id"],"Packaging record submitted by the packaging unit.",metadata)
    connection.execute("UPDATE batches SET status='Packaged' WHERE batch_id=?",(record.batch_id,))
    connection.commit(); connection.close(); return {"ok":True,"batchId":record.batch_id,"status":"Packaged"}

@app.post("/packaging/{batch_id}/verify")
def verify_packaging(batch_id:str,user=Depends(current_user)):
    if user["role"]!="packaging": raise HTTPException(status_code=403,detail="Packaging unit access required")
    connection=db(); require_batch_state(connection,batch_id,["Packaged"])
    add_trace_event(connection,batch_id,"Packaging verified",user["name"],user["id"],"Packaging unit verified the final package and lot record.")
    connection.execute("UPDATE batches SET status='Awaiting Admin Validation' WHERE batch_id=?",(batch_id,))
    connection.commit(); connection.close(); return {"ok":True,"batchId":batch_id,"status":"Awaiting Admin Validation"}

@app.post("/admin/batches/{batch_id}/validate")
def validate_batch(batch_id:str,user=Depends(current_user)):
    if user["role"]!="admin": raise HTTPException(status_code=403,detail="Admin validation required")
    connection=db(); require_batch_state(connection,batch_id,["Awaiting Admin Validation"])
    add_trace_event(connection,batch_id,"Admin validation",user["name"],user["id"],"KVIC authority validated the complete traceability record.")
    connection.execute("UPDATE batches SET status='Validated' WHERE batch_id=?",(batch_id,))
    connection.commit(); connection.close(); return {"ok":True,"batchId":batch_id,"status":"Validated"}

@app.get("/batches/{batch_id}/workflow")
def batch_workflow(batch_id:str,user=Depends(current_user)):
    connection=db(); data=workflow_status(connection,batch_id); connection.close()
    if not data: raise HTTPException(status_code=404,detail="Batch not found")
    return data


# ============================================================
# QR
# ============================================================

def build_qr_svg(public_url):
    from qrcode import QRCode
    from qrcode.image.svg import SvgImage

    qr = QRCode(version=None, box_size=8, border=2)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image(image_factory=SvgImage)
    stream = io.BytesIO()
    img.save(stream)
    return stream.getvalue().decode("utf-8")


def build_qr_data_url(public_url):
    from qrcode import QRCode
    import base64

    qr = QRCode(version=None, box_size=8, border=2)
    qr.add_data(public_url)
    qr.make(fit=True)
    img = qr.make_image()
    stream = io.BytesIO()
    img.save(stream, format="PNG")
    import base64
    return "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode("ascii")


@app.post("/api/batches/{batch_id}/qr")
@app.post("/batches/{batch_id}/qr")
def generate_qr(
    batch_id: str,
    hive_id: Optional[str] = None,
    user=Depends(current_user)
):

    connection = db()

    batch = row(
        connection,
        """
        SELECT batch_id
        FROM batches
        WHERE batch_id = ?
        """,
        (batch_id,)
    )

    if not batch:

        connection.close()

        raise HTTPException(status_code=404, detail="Batch not found")

    full_batch=row(connection,"SELECT status FROM batches WHERE batch_id=?",(batch_id,))
    if user["role"] not in ("packaging","admin"):
        connection.close(); raise HTTPException(status_code=403,detail="QR issuance is available to packaging and admin roles")
    if full_batch and full_batch.get("status") != "Validated":
        connection.close(); raise HTTPException(status_code=409,detail="QR can be issued only after admin validation")

    if hive_id:
        linked = row(connection, "SELECT h.hive_code FROM batches b JOIN hives h ON h.id=b.hive_id WHERE b.batch_id=?", (batch_id,))
        if not linked or linked["hive_code"] != hive_id:
            connection.close()
            raise HTTPException(status_code=400, detail="Selected batch does not belong to the selected hive")

    existing = row(
        connection,
        """
        SELECT *
        FROM qr_codes
        WHERE batch_id = ?
        """,
        (batch_id,)
    )

    if existing:
        public_url = f"{PUBLIC_BASE_URL}/consumer/{batch_id}"
        connection.execute("UPDATE qr_codes SET public_url=? WHERE batch_id=?", (public_url, batch_id))
        connection.commit()
        existing["public_url"] = public_url
        connection.close()
        return {**existing, "qrUrl": public_url, "dashboardUrl": public_url,
                "qrSvg": build_qr_svg(public_url),
                "qrDataUrl": build_qr_data_url(public_url)}

    token = secrets.token_urlsafe(16)

    # Change this to your deployed public HTTPS domain
    # when the project is deployed.
    public_url = f"{PUBLIC_BASE_URL}/consumer/{batch_id}"

    connection.execute(
        """
        INSERT INTO qr_codes
        (
            batch_id,
            qr_token,
            public_url
        )
        VALUES (?, ?, ?)
        """,
        (
            batch_id,
            token,
            public_url
        )
    )

    connection.commit()

    qr = row(
        connection,
        """
        SELECT *
        FROM qr_codes
        WHERE batch_id = ?
        """,
        (batch_id,)
    )

    qr_svg = build_qr_svg(public_url)
    qr_data_url = build_qr_data_url(public_url)
    if not connection.execute("SELECT 1 FROM traceability_events WHERE batch_id=? AND event_type='QR issued' LIMIT 1",(batch_id,)).fetchone():
        add_trace_event(connection,batch_id,"QR issued",user["name"],user["id"],"Public digital passport QR issued for the validated batch.",{"public_url":public_url})
    connection.commit()
    connection.close()

    return {**qr, "qrUrl": public_url, "dashboardUrl": public_url, "qrSvg": qr_svg, "qrDataUrl": qr_data_url}


# ============================================================
# PUBLIC CONSUMER PASSPORT
# ============================================================

def public_batch_passport(batch_id):

    connection = db()

    batch = row(
        connection,
        """
        SELECT
            b.*,
            h.hive_code,
            h.location,
            h.species

        FROM batches b

        LEFT JOIN hives h
            ON b.hive_id = h.id

        WHERE b.batch_id = ?
        """,
        (batch_id,)
    )

    if not batch:

        connection.close()

        raise HTTPException(
            status_code=404,
            detail="Batch not found"
        )

    events = rows(
        connection,
        """
        SELECT
            event_type,
            actor,
            event_date,
            description,
            event_hash,
            previous_hash

        FROM traceability_events

        WHERE batch_id = ?

        ORDER BY id
        """,
        (batch_id,)
    )

    # Record the first public verification once. Repeated QR scans should not
    # create duplicate blockchain blocks.
    existing_verification = connection.execute(
        "SELECT id FROM traceability_events WHERE batch_id=? AND event_type='Consumer verification' LIMIT 1",
        (batch_id,)
    ).fetchone()
    if not existing_verification:
        verification_payload = {
            "batch_id": batch_id,
            "event_type": "Consumer verification",
            "actor": "Public consumer",
            "event_date": datetime.utcnow().date().isoformat(),
            "description": "Digital passport opened for verification."
        }
        connection.execute(
            """INSERT INTO traceability_events
            (batch_id,event_type,actor,actor_id,event_date,description,metadata,event_hash)
            VALUES (?,?,?,?,?,?,?,?)""",
            (batch_id, "Consumer verification", "Public consumer", None,
             verification_payload["event_date"], verification_payload["description"],
             json.dumps({"public": True}), create_event_hash(verification_payload, None))
        )
        verify_event_id = connection.execute(
            "SELECT last_insert_rowid() AS id"
        ).fetchone()["id"]
        add_blockchain_block(connection, batch_id, verify_event_id, verification_payload)
        connection.commit()

    # Verify again after the optional consumer-verification block was added.
    valid_chain, blocks = verify_blockchain_chain(connection, batch_id)

    events = rows(
        connection,
        """SELECT event_type,actor,event_date,description,event_hash,previous_hash
           FROM traceability_events WHERE batch_id=? ORDER BY id""",
        (batch_id,)
    )

    # Trust score is earned only after the workflow is complete.
    event_types={x["event_type"] for x in events}
    required=["Hive source","Harvest recorded","Health assessed","Processing verified","Packaging verified","Admin validation"]
    completed=sum(1 for x in required if x in event_types)
    finalised=completed==len(required) and batch.get("status")=="Validated"
    score=100 if finalised else None
    meaning="Complete traceability and administrative validation. This does not certify honey purity." if finalised else "Trust score is pending until health, processing, packaging and admin validation are completed."

    return {
        "batch": batch,
        "trustScore": score,
        "trustScorePending": not finalised,
        "completedStages": completed,
        "requiredStages": len(required),
        "trustScoreMeaning": meaning,
        "integrityStatus": (
            "Verified"
            if batch.get("integrity_hash")
            else "Incomplete"
        ),
        "timeline": events,
        "workflow": {"completedStages": completed, "requiredStages": len(required), "finalised": finalised},
        "blockchain": {
            "verified": valid_chain,
            "blockCount": len(blocks),
            **blockchain_mirror_status()
        }
    }


@app.get("/public/batches/{batch_id}")
def public_batch(
    batch_id: str
):

    return public_batch_passport(batch_id)


@app.get("/api/public/verify/{batch_id}")
@app.get("/public/verify/{batch_id}")
def public_verify(
    batch_id: str
):

    return public_batch_passport(batch_id)


# ============================================================
# ADMIN / KVIC
# ============================================================

@app.get("/admin/network")
def admin_network(
    user=Depends(require("admin"))
):

    connection = db()

    users = rows(
        connection,
        """
        SELECT
            id,
            name,
            email,
            role,
            organization,
            status
        FROM users
        ORDER BY id
        """
    )

    hives = rows(
        connection,
        """
        SELECT
            hive_code,
            location,
            status,
            species,
            colony_strength,
            temperature,
            humidity,
            weight
        FROM hives
        ORDER BY id
        """
    )

    batches = rows(
        connection,
        """
        SELECT
            batch_id,
            honey_type,
            quantity,
            harvest_date,
            origin,
            status,
            integrity_hash
        FROM batches
        ORDER BY created_at DESC
        """
    )

    recent_events = rows(
        connection,
        """
        SELECT batch_id, event_type, actor, event_date, description
        FROM traceability_events
        ORDER BY id DESC
        LIMIT 12
        """
    )

    connection.close()

    return {
        "users": users,
        "hives": hives,
        "batches": batches,
        "recentEvents": recent_events,
        "network": {
            "totalUsers": len(users),
            "totalHives": len(hives),
            "totalBatches": len(batches)
        }
    }


# ============================================================
# RUN DIRECTLY
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8000,
        reload=True
    )