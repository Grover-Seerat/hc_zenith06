# Low-memory deployment notes

This version is optimized for small containers (including a 512 MB Render Free
container):

- ML modules are no longer imported when FastAPI starts.
- Bee CNN and EfficientNet are stored as TorchScript and loaded only for an
  image inference request.
- Visual models are executed sequentially and released after each prediction,
  instead of keeping all four models in RAM at once.
- PyTorch/native numerical thread pools are limited to one thread.
- Web3, QR generation, pandas/joblib production inference are lazy-loaded.
- The Docker image no longer tries to copy the repository-level `blockchain/`
  directory while the build context is `/backend`.
- Uvicorn is explicitly single-worker.

The model files remain in `backend/app/ml/models/`; no model is removed from
the application. The original `.pth`/`.pt` files are retained for provenance,
while the two TorchScript files are used by the memory-safe image pipeline.
