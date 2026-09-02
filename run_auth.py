"""Launch AetherSwap's isolated Steam authentication preparation surface."""

from __future__ import annotations

import uvicorn


if __name__ == "__main__":
    uvicorn.run(
        "app.auth_bootstrap:app",
        host="127.0.0.1",
        port=8766,
        reload=False,
        access_log=False,
    )
