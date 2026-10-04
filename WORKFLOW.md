# HoneyChain live transition workflow

The interface is intentionally preserved. The data behind it is now treated as a workflow rather than pre-filled display information.

1. Beekeeper creates a hive.
2. Beekeeper runs Bee Health / Hive Health analysis and live IoT readings can contribute sensor evidence.
3. Beekeeper creates a harvest batch. Its status is `Harvested`; it does not receive a final trust score.
4. The health analysis creates a `Health assessed` event for the linked batch.
5. Processor sees only eligible batches, records actual processing data and then explicitly verifies processing.
6. Packaging unit sees only processor-verified batches, records package size, quantity and lot number, then explicitly verifies packaging.
7. Admin sees the `Awaiting Admin Validation` queue and validates the completed journey.
8. QR issuance is allowed only after admin validation. QR points to the public consumer passport.
9. Every transition writes a traceability event and a chained SHA-256 blockchain block. Polygon anchoring is optional but live when configured.
10. Consumer passport shows the actual recorded journey. Trust score remains `Pending` until the required stages are complete; after final validation it can become a completeness/integrity score.

This prevents the system from claiming that processing, packaging or validation happened before a responsible stakeholder actually recorded it.
