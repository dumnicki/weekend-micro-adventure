from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.routes import router

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Weekend Micro-Adventure")
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """The one screen. Mounted after the router so /api keeps priority."""
    return FileResponse(STATIC_DIR / "index.html")