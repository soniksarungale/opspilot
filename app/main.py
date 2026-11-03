from fastapi import FastAPI

app = FastAPI(title="OpsPilot")


@app.get("/api/health")
def health():
    return {"ok": True}
