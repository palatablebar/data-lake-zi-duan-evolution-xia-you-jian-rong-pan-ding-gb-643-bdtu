"""迁移单编排：账目、迁移单、导出物。

这里是对外的账目出口，判定细节不重复实现，只汇总本次运行的结果。
"""

from __future__ import annotations

from app import manifest as manifest_mod
from app.modules.m7_migration import csv_out, ledger, publish, spec_md, temporal


def build_ledger(diff: dict, consumers: list[dict], resolution: dict) -> dict:
    return ledger.build(diff, consumers, resolution)


def temporal_ruling(run: dict) -> dict:
    return run.get("temporal_ruling") or temporal.build(run)


def temporal_csv_text(run: dict) -> str:
    return csv_out.temporal_csv(temporal_ruling(run))


def spec_markdown(run: dict) -> str:
    return spec_md.render(run)


def clauses(run: dict) -> list[dict]:
    return spec_md.clauses(run)


def fields_csv_text(run: dict) -> str:
    return csv_out.fields_csv(run.get("diff") or [])


def refs_csv_text(run: dict) -> str:
    return csv_out.refs_csv(run)


def ledger_csv_text(run: dict) -> str:
    return csv_out.ledger_csv(run.get("ledger") or {})


def manifest_json_text(run: dict) -> str:
    from datetime import datetime

    created = run.get("created_at")
    moment = datetime.fromisoformat(created) if created else None
    payload = manifest_mod.build_manifest(run, moment) if moment else {}
    payload["input_names"] = run.get("input_names") or {}
    payload["ledger"] = run.get("ledger") or {}
    payload["warnings"] = run.get("warnings") or []
    # 运行清单与迁移单共用同一份裁决，避免两处结论不一致。
    payload["temporal_ruling"] = temporal_ruling(run)
    return csv_out.manifest_json(payload)


def latest_or_404() -> dict:
    """导出读取当前迁移单：优先已发布的，没有发布记录时退回最新批次。"""
    run = publish.current_run()
    if not run:
        from app import errors

        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    return run


def current_run() -> dict | None:
    return publish.current_run()


def publish_run(run_id: str | None = None, replay: list[dict] | None = None) -> dict:
    return publish.publish(run_id, replay)


def rollback_run() -> dict:
    return publish.rollback()


def publish_state() -> dict:
    return publish.state()
