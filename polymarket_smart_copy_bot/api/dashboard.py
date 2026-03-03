from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, Response

router = APIRouter(tags=["dashboard"])

_DIST_DIR = Path(__file__).resolve().parents[1] / "dist"
_INDEX_FILE = _DIST_DIR / "index.html"


@router.get("/dashboard")
async def dashboard() -> Response:
    """Serve React dashboard bundle.

    If frontend assets were not built yet, return a concise hint for local setup.
    """

    if _INDEX_FILE.exists():
        return FileResponse(_INDEX_FILE)

    return HTMLResponse(
        """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Dashboard build is missing</title>
    <style>
      body { background:#0b1020; color:#eaf0ff; font-family: Inter, system-ui, sans-serif; margin:0; }
      main { max-width: 780px; margin: 40px auto; padding: 24px; border:1px solid #2a3c71; border-radius: 12px; background: #111931; }
      code { background:#0f1730; border:1px solid #2a3c71; border-radius:6px; padding:2px 6px; }
    </style>
  </head>
  <body>
    <main>
      <h2>Frontend bundle not found</h2>
      <p>Build dashboard assets first:</p>
      <p><code>npm install && npm run build</code></p>
      <p>Then reload <code>/dashboard</code>.</p>
    </main>
  </body>
</html>
        """.strip()
    )
