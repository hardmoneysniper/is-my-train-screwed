from fastapi import FastAPI
from app.api.trip import router as trip_router
from app.api.chat import router as chat_router

app = FastAPI(title="Is My Train Screwed?")
app.include_router(trip_router)
app.include_router(chat_router)

@app.get("/health")
def health():
    return {"status": "ok"}
