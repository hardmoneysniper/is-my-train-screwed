from fastapi import FastAPI
from app.api.trip import router as trip_router

app = FastAPI(title="Is My Train Screwed?")
app.include_router(trip_router)

@app.get("/health")
def health():
    return {"status": "ok"}
