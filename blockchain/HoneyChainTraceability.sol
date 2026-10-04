// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract HoneyChainTraceability {
    address public owner;

    struct Record {
        bytes32 eventHash;
        string batchId;
        string eventType;
        uint256 timestamp;
    }

    Record[] private records;
    mapping(bytes32 => bool) public anchored;

    event RecordAnchored(bytes32 indexed eventHash, string batchId, string eventType, uint256 timestamp);

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    function recordEvent(bytes32 eventHash, string calldata batchId, string calldata eventType) external onlyOwner {
        require(!anchored[eventHash], "Already anchored");
        anchored[eventHash] = true;
        records.push(Record(eventHash, batchId, eventType, block.timestamp));
        emit RecordAnchored(eventHash, batchId, eventType, block.timestamp);
    }

    function getRecordCount() external view returns (uint256) {
        return records.length;
    }

    function getRecord(uint256 index) external view returns (bytes32, string memory, string memory, uint256) {
        Record memory r = records[index];
        return (r.eventHash, r.batchId, r.eventType, r.timestamp);
    }
}
