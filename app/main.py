from fastapi import FastAPI

from app.routes import router

app = FastAPI(title="Weekend Micro-Adventure")
app.include_router(router)
