"""Application composition and local entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import api, branch_views, project_views, setup_views, views
from backend.config import Settings
from backend.council_settings import CouncilSettingsStore
from backend.credentials import CredentialsStore
from backend.projects import ProjectStore
from backend.rendering import FRONTEND_DIR, render
from backend.runs import RunError, RunManager
from backend.storage import ConversationStore


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    application_settings = Settings(**({"data_dir": data_dir} if data_dir is not None else {}))
    data_dir = application_settings.data_dir
    if data_dir.is_relative_to((FRONTEND_DIR / "static").resolve()):
        raise ValueError("DATA_DIR must be outside the public static directory.")
    store = ConversationStore(data_dir)
    settings = CouncilSettingsStore(Path(data_dir) / "settings" / "council.json")
    credentials = CredentialsStore(Path(data_dir) / "settings" / "credentials.json")
    projects = ProjectStore(Path(data_dir) / "projects" / "index.json")
    runs = RunManager(store, settings, projects, credentials)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await runs.close()

    app = FastAPI(title="LLM Council", lifespan=lifespan)
    app.state.application_settings = application_settings
    app.state.store = store
    app.state.runs = runs
    app.state.council_settings = settings
    app.state.projects = projects
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")
    app.include_router(api.router)
    app.include_router(views.router)
    app.include_router(branch_views.router)
    app.include_router(setup_views.router)
    app.include_router(project_views.router)

    @app.exception_handler(RunError)
    async def run_error(request: Request, error: RunError):
        if request.url.path.startswith("/api/"):
            return JSONResponse(
                {"detail": str(error), "setup_required": error.setup_required},
                status_code=error.status_code,
            )
        if request.headers.get("HX-Request") == "true":
            return HTMLResponse(
                render("notice.html", error=str(error), setup_required=error.setup_required),
                status_code=error.status_code,
                headers={"HX-Retarget": "#request-error", "HX-Reswap": "innerHTML"},
            )
        return HTMLResponse(
            render("error.html", error=str(error), setup_required=error.setup_required),
            status_code=error.status_code,
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "LLM Council API"}

    return app


app = create_app()


def main():
    import uvicorn

    settings = app.state.application_settings
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
