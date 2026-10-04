from pathlib import Path
p=Path('/mnt/data/hcwork/backend/app/main.py')
s=p.read_text()
# imports
s=s.replace('import qrcode\nfrom qrcode.image.svg import SvgImage\n', 'import qrcode\nfrom qrcode.image.svg import SvgImage\n\ntry:\n    from web3 import Web3\nexcept Exception:\n    Web3 = None\n')
# config
s=s.replace("PUBLIC_BASE_URL = os.getenv('HONEYCHAIN_PUBLIC_BASE_URL', 'http://localhost:5173').rstrip('/')\n", "PUBLIC_BASE_URL = os.getenv('HONEYCHAIN_PUBLIC_BASE_URL', 'http://localhost:5173').rstrip('/')\nBLOCKCHAIN_RPC_URL = os.getenv('HONEYCHAIN_BLOCKCHAIN_RPC_URL', '').strip()\nBLOCKCHAIN_PRIVATE_KEY = os.getenv('HONEYCHAIN_BLOCKCHAIN_PRIVATE_KEY', '').strip()\nBLOCKCHAIN_CONTRACT_ADDRESS = os.getenv('HONEYCHAIN_BLOCKCHAIN_CONTRACT_ADDRESS', '').strip()\nBLOCKCHAIN_EXPLORER_URL = os.getenv('HONEYCHAIN_BLOCKCHAIN_EXPLORER_URL', '').strip().rstrip('/')\n")
# replace blockchain functions
start=s.index('def add_blockchain_block(')
end=s.index('# ============================================================\n# DATABASE INITIALIZATION', start)
new=r'''def _evm_client():
    if not (Web3 and BLOCKCHAIN_RPC_URL and BLOCKCHAIN_PRIVATE_KEY and BLOCKCHAIN_CONTRACT_ADDRESS):
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(BLOCKCHAIN_RPC_URL, request_kwargs={"timeout": 8}))
        if not w3.is_connected():
            return None
        return w3
    except Exception:
        return None


def _onchain_record(block_hash: str, batch_id: str, event_type: str):
    """Optional production EVM anchor. Local hash-chain remains the source of truth if unset."""
    w3 = _evm_client()
    if not w3:
        return None
    try:
        abi = [{"inputs":[{"internalType":"bytes32","name":"eventHash","type":"bytes32"},{"internalType":"string","name":"batchId","type":"string"},{"internalType":"string","name":"eventType","type":"string"}],"name":"recordEvent","outputs":[],"stateMutability":"nonpayable","type":"function"}]
        contract = w3.eth.contract(address=Web3.to_checksum_address(BLOCKCHAIN_CONTRACT_ADDRESS), abi=abi)
        account = w3.eth.account.from_key(BLOCKCHAIN_PRIVATE_KEY)
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = contract.functions.recordEvent(bytes.fromhex(block_hash), batch_id, event_type).build_transaction({
            'from': account.address, 'nonce': nonce, 'chainId': w3.eth.chain_id,
            'gas': 220000, 'maxFeePerGas': w3.to_wei('30', 'gwei'), 'maxPriorityFeePerGas': w3.to_wei('1', 'gwei')
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
    return {
        "mode": "Public EVM blockchain anchor + local hash chain" if BLOCKCHAIN_RPC_URL and BLOCKCHAIN_PRIVATE_KEY and BLOCKCHAIN_CONTRACT_ADDRESS else "HoneyChain tamper-evident hash-linked ledger",
        "chainConfigured": bool(BLOCKCHAIN_RPC_URL and BLOCKCHAIN_PRIVATE_KEY and BLOCKCHAIN_CONTRACT_ADDRESS),
        "fabricConfigured": bool(FABRIC_GATEWAY_URL),
        "explorerUrl": BLOCKCHAIN_EXPLORER_URL or None
    }

'''
s=s[:start]+new+s[end:]
# table migration add columns after create table
needle='''        CREATE TABLE IF NOT EXISTS blockchain_blocks (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            block_index INTEGER UNIQUE NOT NULL,\n            batch_id TEXT NOT NULL,\n            event_id INTEGER,\n            payload TEXT NOT NULL,\n            previous_hash TEXT NOT NULL,\n            block_hash TEXT UNIQUE NOT NULL,\n            created_at TEXT DEFAULT CURRENT_TIMESTAMP\n        );'''
replace=needle+'''\n        ''\n'''
# don't use malformed SQL; instead insert migration after executescript closing marker via a unique comment
# Locate connection.executescript closing just before USERS
marker='''    )\n\n    # --------------------------------------------------------\n    # USERS\n'''
migration='''    )\n\n    # Lightweight schema migration for deployed SQLite databases.\n    existing_cols = {r["name"] for r in connection.execute("PRAGMA table_info(blockchain_blocks)").fetchall()}\n    if "level" not in existing_cols:\n        connection.execute("ALTER TABLE blockchain_blocks ADD COLUMN level TEXT")\n    if "onchain_tx_hash" not in existing_cols:\n        connection.execute("ALTER TABLE blockchain_blocks ADD COLUMN onchain_tx_hash TEXT")\n\n    # --------------------------------------------------------\n    # USERS\n'''
s=s.replace(marker,migration,1)
# add hive block after insert history/production commit area
old='''    connection.execute(\n        "INSERT INTO production_history (hive_id, period, production) VALUES (?, ?, ?)",\n        (hive_db_id, "Current", round(max(0, hive.weight * 0.42), 1))\n    )\n    connection.commit()'''
new='''    connection.execute(\n        "INSERT INTO production_history (hive_id, period, production) VALUES (?, ?, ?)",\n        (hive_db_id, "Current", round(max(0, hive.weight * 0.42), 1))\n    )\n    add_blockchain_block(connection, f"HIVE:{hive_code}", hive_db_id, {\n        "level": "Hive registration", "hive_id": hive_code, "actor": user["name"],\n        "event_type": "Hive registration", "event_date": datetime.utcnow().isoformat(),\n        "description": "Hive registered in HoneyChain."\n    }, level="Hive registration")\n    connection.commit()'''
s=s.replace(old,new,1)
# sensor sync add block before commit
old='''    connection.execute('UPDATE hives SET temperature=?, humidity=? WHERE id=?',\n                       (reading['temperature'], reading['humidity'], hive['id']))\n    connection.commit(); connection.close()\n    return {'connected': True, 'hiveId': hive_code, **reading}'''
new='''    connection.execute('UPDATE hives SET temperature=?, humidity=? WHERE id=?',\n                       (reading['temperature'], reading['humidity'], hive['id']))\n    sensor_event_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]\n    add_blockchain_block(connection, f"HIVE:{hive_code}", sensor_event_id, {\n        "level": "IoT reading", "hive_id": hive_code, "event_type": "IoT reading",\n        "event_date": reading["timestamp"], "temperature": reading["temperature"], "humidity": reading["humidity"],\n        "description": "Live Wi-Fi sensor reading recorded."\n    }, level="IoT reading")\n    connection.commit(); connection.close()\n    return {'connected': True, 'hiveId': hive_code, **reading}'''
s=s.replace(old,new,1)
# health analysis block after save result before update
old='''    bee_score,bee_status,varroa_risk=_save_health_result(connection,hive["id"],image.filename,visual,hive_result)\n    connection.execute("UPDATE hives SET temperature=COALESCE(?,temperature), humidity=COALESCE(?,humidity), weight=COALESCE(?,weight) WHERE id=?",(temperature,humidity,weight,hive["id"]))'''
new='''    bee_score,bee_status,varroa_risk=_save_health_result(connection,hive["id"],image.filename,visual,hive_result)\n    analysis_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]\n    add_blockchain_block(connection, f"HIVE:{hive_id}", analysis_id, {\n        "level": "Health analysis", "hive_id": hive_id, "event_type": "Health analysis",\n        "event_date": datetime.utcnow().isoformat(), "bee_health_score": bee_score,\n        "hive_score": hive_result.get("score"), "temperature": hive_result.get("temperature"),\n        "humidity": hive_result.get("humidity"), "model": hive_result.get("model"),\n        "description": "Bee and hive health evidence analysis recorded."\n    }, level="Health analysis")\n    connection.execute("UPDATE hives SET temperature=COALESCE(?,temperature), humidity=COALESCE(?,humidity), weight=COALESCE(?,weight) WHERE id=?",(temperature,humidity,weight,hive["id"]))'''
s=s.replace(old,new,1)
# QR add block after QR insertion locate generated block commit
old='''    connection.execute(\n        """INSERT INTO qr_codes\n        (batch_id, qr_token, public_url)\n        VALUES (?, ?, ?)\n        """,\n        (batch_id, token, public_url)\n    )\n\n    connection.commit()'''
new='''    connection.execute(\n        """INSERT INTO qr_codes\n        (batch_id, qr_token, public_url)\n        VALUES (?, ?, ?)\n        """,\n        (batch_id, token, public_url)\n    )\n    qr_event_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]\n    add_blockchain_block(connection, batch_id, qr_event_id, {\n        "level": "QR generation", "batch_id": batch_id, "event_type": "QR generation",\n        "event_date": datetime.utcnow().isoformat(), "public_url": public_url,\n        "description": "Public consumer QR generated for batch passport."\n    }, level="QR generation")\n\n    connection.commit()'''
s=s.replace(old,new,1)
# get blockchain endpoint add overview and levels
insert='''\n@app.get("/api/blockchain/overview")\ndef blockchain_overview(user=Depends(current_user)):\n    if user["role"] != "admin":\n        raise HTTPException(status_code=403, detail="Blockchain Trace is available to the admin workspace")\n    connection = db()\n    valid, blocks = verify_blockchain_chain(connection)\n    counts = rows(connection, "SELECT COALESCE(level,'Traceability') AS level, COUNT(*) AS count FROM blockchain_blocks GROUP BY COALESCE(level,'Traceability') ORDER BY MIN(block_index)")\n    connection.close()\n    return {"verified": valid, "blockCount": len(blocks), "blocks": blocks, "levels": counts, **blockchain_mirror_status()}\n\n'''
pos=s.index('@app.get("/api/qr/{batch_id}")')
s=s[:pos]+insert+s[pos:]
# add CSV endpoint before IoT reading endpoints
csv_endpoint=r'''
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

'''
pos=s.index('@app.get("/api/iot/readings/{hive_code}")')
s=s[:pos]+csv_endpoint+s[pos:]
p.write_text(s)
