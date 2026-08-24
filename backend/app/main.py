from fastapi import FastAPI

app = FastAPI(title="Is My Train Screwed?")

@app.get("/health")
def health():
    return {"status": "ok"}
