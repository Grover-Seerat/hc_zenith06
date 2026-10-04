import json, os
from pathlib import Path
from web3 import Web3
from solcx import compile_standard, install_solc

ROOT = Path(__file__).resolve().parent
source = (ROOT / 'HoneyChainTraceability.sol').read_text()
install_solc('0.8.20')
compiled = compile_standard({
    'language':'Solidity',
    'sources':{'HoneyChainTraceability.sol':{'content':source}},
    'settings':{'outputSelection':{'*':{'*':['abi','evm.bytecode']}}}
}, solc_version='0.8.20')
contract = compiled['contracts']['HoneyChainTraceability.sol']['HoneyChainTraceability']
abi = contract['abi']
bytecode = contract['evm']['bytecode']['object']

rpc = os.environ['HONEYCHAIN_BLOCKCHAIN_RPC_URL']
private_key = os.environ['HONEYCHAIN_BLOCKCHAIN_PRIVATE_KEY']
w3 = Web3(Web3.HTTPProvider(rpc))
assert w3.is_connected(), 'Cannot connect to RPC'
account = w3.eth.account.from_key(private_key)
factory = w3.eth.contract(abi=abi, bytecode=bytecode)
nonce = w3.eth.get_transaction_count(account.address)
tx = factory.constructor().build_transaction({
    'from': account.address, 'nonce': nonce, 'chainId': w3.eth.chain_id,
    'gas': 1800000, 'gasPrice': w3.eth.gas_price
})
signed = account.sign_transaction(tx)
hash_ = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(hash_)
address = receipt.contractAddress
print('Contract address:', address)
print('Deployment tx:', receipt.transactionHash.hex())
(Path(__file__).parent / 'HoneyChainTraceability.abi.json').write_text(json.dumps(abi, indent=2))
print('ABI saved to blockchain/HoneyChainTraceability.abi.json')
