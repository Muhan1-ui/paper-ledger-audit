"""FastAPI read-only service for TaiCaiTracker query projections."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .review_agent import build_review
from .service_store import create_store


_DASHBOARD_HTML_PATH = Path(__file__).with_name("static") / "dashboard.html"


def create_app(database_url=None, store=None, record_loader=None, summary_loader=None):
    """Create a read-only HTTP API backed by the relational projection store."""
    if store is None:
        store = create_store(database_url)
    if record_loader is None:
        record_loader = lambda: []
    if not callable(record_loader):
        raise ValueError("record_loader 必须是可调用对象")
    if summary_loader is not None and not callable(summary_loader):
        raise ValueError("summary_loader 必须是可调用对象")
    app = FastAPI(
        title="TaiCaiTracker Service",
        version="0.1.0",
        description="Read-only query service for auditable paper-ledger projections.",
    )

    def load_review(match_id=None):
        try:
            return build_review(record_loader(), match_id=match_id)
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=503, detail="复盘数据暂不可用") from error

    def load_summary():
        if summary_loader is None:
            raise HTTPException(status_code=503, detail="仪表盘数据暂不可用")
        try:
            return summary_loader()
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=503, detail="仪表盘数据暂不可用") from error

    @app.get("/", response_class=FileResponse)
    def dashboard():
        return FileResponse(_DASHBOARD_HTML_PATH, media_type="text/html")

    @app.get("/api/health")
    def health():
        store.health_check()
        return {"status": "ok", "database": "connected"}

    @app.get("/api/matches")
    def list_matches(limit: int = Query(default=20, ge=1, le=100)):
        total, items = store.list_matches(limit=limit)
        return {"total": total, "items": items}

    @app.get("/api/matches/{match_id}")
    def match_detail(match_id: str):
        detail = store.match_detail(match_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="赛事不存在")
        return detail

    @app.get("/api/reviews")
    def global_review():
        return load_review()

    @app.get("/api/summary")
    def dashboard_summary():
        return load_summary()

    @app.get("/api/matches/{match_id}/review")
    def match_review(match_id: str):
        review = load_review(match_id)
        if review["scope"]["matched_predictions"] == 0:
            raise HTTPException(status_code=404, detail="赛事不存在")
        return review

    return app
