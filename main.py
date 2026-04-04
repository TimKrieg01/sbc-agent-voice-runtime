import logging
from fastapi import FastAPI
from dotenv import load_dotenv

# Load .env file automatically
load_dotenv()

from src.api.routes import router
from src.api.sip_routes import router as sip_router
from src.services.sip.session_manager import sip_session_manager

# Setup global logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = FastAPI(title="Agentic SIP Trunk Microservices")

# Mount API routes
app.include_router(router)
app.include_router(sip_router)


@app.on_event("shutdown")
async def on_shutdown():
    await sip_session_manager.stop_all()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
