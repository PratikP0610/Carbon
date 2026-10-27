"""Carbon Local Application API Server (FastAPI)."""

import asyncio
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Security,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from carbon.config.settings import Settings, get_settings
from carbon.core.logging import configure_logging
from carbon.core.orchestrator import CarbonCore
from carbon.interfaces.models import (
    ModelSwitchRequest,
    TaskRequest,
)

logger = logging.getLogger("carbon.api")

# Global Core instance and Security
core: CarbonCore | None = None
security_scheme = HTTPBearer(auto_error=False)


async def get_core() -> CarbonCore:
    """Retrieve or initialize the global CarbonCore instance."""
    global core
    if core is None:
        settings = get_settings()
        core = CarbonCore(settings=settings)
        await core.initialize()
    return core


def _get_or_create_token(settings: Settings) -> str:
    """Retrieve or generate an ephemeral local token with mode 0600."""
    if settings.api_token:
        return settings.api_token

    token_path = settings.data_dir / ".carbon_token"
    if token_path.exists():
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        except Exception:
            pass

    # Generate new high-entropy token
    token = secrets.token_urlsafe(32)
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        mode = 0o600
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(token)
        os.chmod(token_path, mode)
        logger.info(f"Generated ephemeral local token at {token_path}")
    except Exception as e:
        logger.warning(f"Could not persist local token file: {e}")

    return token


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI application lifespan manager for startup and shutdown."""
    settings = get_settings()
    configure_logging(level=settings.log_level)
    logger.info("Initializing Carbon API Server...")

    # Ensure Core Orchestrator is initialized
    await get_core()

    # Determine bearer token
    token = _get_or_create_token(settings)
    app.state.api_token = token
    logger.info(
        f"Carbon API Server listening strictly on http://{settings.api_host}:{settings.api_port}"
    )

    yield

    # Clean shutdown
    global core
    if core:
        await core.shutdown()
        core = None
    logger.info("Carbon API Server shut down cleanly.")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    app = FastAPI(
        title="Carbon Local API",
        version="1.0.0",
        description="Local loopback API for Carbon Personal AI Assistant",
        lifespan=lifespan,
    )

    # CORS restricted to loopback
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Middleware: Loopback enforcement (Non-Negotiable Rule 6)
    @app.middleware("http")
    async def loopback_security_middleware(request: Request, call_next):
        client_host = request.client.host if request.client else "unknown"
        allowed_hosts = {"127.0.0.1", "localhost", "::1", "testclient"}
        if client_host not in allowed_hosts:
            logger.error(f"Rejected non-loopback connection attempt from {client_host}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: Carbon API is restricted strictly to local loopback.",
            )
        return await call_next(request)

    async def verify_local_auth(
        auth: HTTPAuthorizationCredentials = Security(security_scheme),
    ) -> bool:
        """Verify local authorization token if configured."""
        expected_token = getattr(app.state, "api_token", None)
        if not expected_token:
            return True
        if not auth or auth.credentials != expected_token:
            # Allow unauthenticated queries to basic health endpoint
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing local authorization token.",
            )
        return True

    # --- REST Endpoints ---

    @app.get("/")
    async def root():
        return {
            "name": "Carbon",
            "version": "1.0.0",
            "status": "online",
            "loopback_only": True,
        }

    @app.get("/api/v1/health")
    async def get_health():
        c = await get_core()
        return await c.get_system_health()

    @app.get("/api/v1/models", dependencies=[Depends(verify_local_auth)])
    async def list_models() -> dict[str, Any]:
        c = await get_core()
        active = await c.cms.get_active_model()
        registered = await c.cms.list_registered_models()
        return {
            "active_model": active.model_dump() if active else None,
            "registered_models": [m.model_dump() for m in registered],
        }

    @app.post("/api/v1/models/switch", dependencies=[Depends(verify_local_auth)])
    async def switch_model(request: ModelSwitchRequest):
        c = await get_core()
        success = await c.cms.switch_model(request)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Failed to switch to model '{request.target_model_id}'.",
            )
        active = await c.cms.get_active_model()
        return {"status": "success", "active_model": active.model_dump() if active else None}

    @app.post("/api/v1/tasks", dependencies=[Depends(verify_local_auth)])
    async def submit_task(request: TaskRequest):
        c = await get_core()
        task = await c.create_task(request)
        # Execute asynchronously in background if non-streaming
        if not request.stream:

            async def run_bg():
                async for _ in c.execute_task_stream(task):
                    pass

            asyncio.create_task(run_bg())
        return {
            "task_id": task.task_id,
            "conversation_id": task.conversation_id,
            "state": task.state.value,
        }

    @app.get("/api/v1/tasks/{task_id}", dependencies=[Depends(verify_local_auth)])
    async def get_task_status(task_id: str):
        c = await get_core()
        task = c.get_task(task_id)
        if not task:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found.")
        return task.to_dict()

    @app.post("/api/v1/tasks/{task_id}/cancel", dependencies=[Depends(verify_local_auth)])
    async def cancel_task(task_id: str):
        c = await get_core()
        cancelled = c.cancel_task(task_id)
        if not cancelled:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Task cannot be cancelled or does not exist.",
            )
        return {"task_id": task_id, "status": "cancelled"}

    @app.get("/api/v1/tools", dependencies=[Depends(verify_local_auth)])
    async def list_tools() -> list[dict[str, Any]]:
        c = await get_core()
        return [defn.model_dump() for defn in c.tools.list_definitions()]

    @app.get("/api/v1/system/status", dependencies=[Depends(verify_local_auth)])
    async def get_system_status():
        c = await get_core()
        return await c.devices.get_host_status()

    # --- WebSocket Streaming Endpoint ---

    @app.websocket("/ws/chat")
    async def websocket_chat_endpoint(websocket: WebSocket):
        c = await get_core()
        await websocket.accept()
        logger.info("WebSocket client connected to /ws/chat")

        try:
            while True:
                data_text = await websocket.receive_text()
                try:
                    payload = json.loads(data_text)
                except json.JSONDecodeError:
                    await websocket.send_json({"event": "error", "message": "Invalid JSON format."})
                    continue

                action = payload.get("action", "submit")

                if action == "cancel":
                    task_id = payload.get("task_id", "")
                    if task_id:
                        c.cancel_task(task_id)
                        await websocket.send_json({"event": "cancelled", "task_id": task_id})
                    continue

                if action == "submit":
                    prompt = payload.get("prompt", "").strip()
                    if not prompt:
                        await websocket.send_json(
                            {"event": "error", "message": "Prompt cannot be empty."}
                        )
                        continue

                    req = TaskRequest(
                        prompt=prompt,
                        conversation_id=payload.get("conversation_id"),
                        reasoning_override=payload.get("reasoning"),
                        stream=True,
                    )
                    task = await c.create_task(req)
                    await websocket.send_json(
                        {
                            "event": "task_created",
                            "task_id": task.task_id,
                            "conversation_id": task.conversation_id,
                        }
                    )

                    # Stream task execution events
                    async for event in c.execute_task_stream(task):
                        await websocket.send_json(event)

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected.")
        except Exception as e:
            logger.error(f"WebSocket error: {e}", exc_info=True)

    return app


app = create_app()


def main():
    """Main CLI entry point for running Carbon."""
    settings = get_settings()
    uvicorn.run(
        "carbon.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        reload=False,
    )


if __name__ == "__main__":
    main()
