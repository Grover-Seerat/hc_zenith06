# HoneyChain + Polygon Amoy

HoneyChain can run with the local SHA-256 ledger by default and can additionally anchor every ledger block to Polygon.

## Polygon Amoy demo deployment

Polygon Amoy is the current Polygon PoS testnet for dApp and smart-contract testing. Its chain ID is **80002**, and Polygon documents the RPC as `https://rpc-amoy.polygon.technology/`. The network uses POL as its gas token.

### 1. Create a deployment wallet

Create a dedicated wallet for the HoneyChain contract/anchor service. Never put the private key in frontend code or commit it to Git.

### 2. Fund it with Amoy POL

The deployment/anchor wallet needs test POL for contract deployment and later `recordEvent` transactions.

### 3. Set backend environment variables

Copy `backend/.env.example` to your deployment environment and set:

```text
HONEYCHAIN_BLOCKCHAIN_RPC_URL=https://rpc-amoy.polygon.technology/
HONEYCHAIN_BLOCKCHAIN_PRIVATE_KEY=YOUR_DEPLOYMENT_WALLET_PRIVATE_KEY
HONEYCHAIN_BLOCKCHAIN_CONTRACT_ADDRESS=
HONEYCHAIN_BLOCKCHAIN_EXPLORER_URL=https://amoy.polygonscan.com
```

For a hosted deployment, a private RPC provider can be used instead of the public RPC. Polygon notes that public RPC endpoints may have limits; private providers can require an API key.

### 4. Deploy the contract

From the project root:

```powershell
cd blockchain
python deploy.py
```

The script prints the contract address and deployment transaction and writes `HoneyChainTraceability.abi.json`.

Put the printed contract address into:

```text
HONEYCHAIN_BLOCKCHAIN_CONTRACT_ADDRESS=0x...
```

Restart the backend.

### 5. Verify in HoneyChain

Admin → Blockchain Trace.

The page should show:

- Public network: Polygon Amoy Testnet
- Chain ID: 80002
- Contract address
- Anchor wallet
- Local hash-chain verification
- Public transaction hashes for anchored events

If old local blocks existed before Polygon was configured, use **Anchor existing blocks** from the Blockchain Trace page. New HoneyChain events will attempt to anchor automatically.

## Important deployment distinction

This makes the blockchain layer a real public-network integration, but the application database remains the operational source of traceability data. The smart contract stores the cryptographic event hash plus batch/event identifiers; sensitive or large application data remains off-chain.

Do not claim that HoneyChain is on Polygon until a contract is actually deployed and at least one HoneyChain event has a confirmed Polygon transaction.
