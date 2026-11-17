"""M7 入口：迁移单页与导出物。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import errors, web
from app.modules.m7_migration import service

router = APIRouter()

CSV_HEADERS = {"Content-Disposition": 'attachment; filename="fieldmap.csv"'}


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/spec", response_class=HTMLResponse)
def spec_page(request: Request):
    # 页面保留空态；导出才要求必须有当前迁移单。
    run = service.current_run()
    ruling = service.temporal_ruling(run) if run else {}
    return web.page_or_404(
        request,
        "spec.html",
        "spec",
        run,
        clauses=service.clauses(run) if run else [],
        ledger=(run or {}).get("ledger") or {},
        aliases=((run or {}).get("mapping") or {}).get("aliases") or [],
        warnings=(run or {}).get("warnings") or [],
        temporal=ruling,
        publish_state=service.publish_state(),
    )


@router.get("/api/export/spec.md")
def export_spec():
    run = service.latest_or_404()
    return Response(
        content=service.spec_markdown(run),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run["run_id"]}.md"'},
    )


@router.get("/api/export/fields.csv")
def export_fields():
    run = service.latest_or_404()
    return _csv(service.fields_csv_text(run), "fieldmap-fields.csv")


@router.get("/api/export/refs.csv")
def export_refs():
    run = service.latest_or_404()
    return _csv(service.refs_csv_text(run), "fieldmap-refs.csv")


@router.get("/api/export/ledger.csv")
def export_ledger():
    run = service.latest_or_404()
    return _csv(service.ledger_csv_text(run), "fieldmap-ledger.csv")


@router.get("/api/export/temporal.csv")
def export_temporal():
    run = service.latest_or_404()
    return _csv(service.temporal_csv_text(run), "fieldmap-temporal.csv")


@router.get("/api/temporal")
def temporal_api():
    run = service.latest_or_404()
    return {
        "run_id": run["run_id"],
        "ruling": service.temporal_ruling(run),
        "publish": service.publish_state(),
    }


@router.post("/api/temporal/replay")
def temporal_replay(payload: dict | None = None):
    """回放核验：逐条给出旧/新业务键，任一改变即拒绝。"""
    body = payload or {}
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise errors.BadInput("rows 必须是数组", detail={"field": "rows"})
    if len(rows) > 2000:
        raise errors.BadInput("一次最多核验 2000 行", detail={"count": len(rows)})
    from app.modules.m7_migration import temporal

    return temporal.replay_compare(rows)


@router.post("/api/spec/publish")
def spec_publish(payload: dict | None = None):
    body = payload or {}
    replay = body.get("replay")
    if replay is not None and not isinstance(replay, list):
        raise errors.BadInput("replay 必须是数组", detail={"field": "replay"})
    return service.publish_run(body.get("run_id"), replay)


@router.post("/api/spec/rollback")
def spec_rollback():
    return service.rollback_run()


@router.get("/api/spec/state")
def spec_state():
    return service.publish_state()


@router.get("/api/export/manifest.json")
def export_manifest():
    run = service.latest_or_404()
    return Response(
        content=service.manifest_json_text(run),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run["run_id"]}.json"'},
    )
