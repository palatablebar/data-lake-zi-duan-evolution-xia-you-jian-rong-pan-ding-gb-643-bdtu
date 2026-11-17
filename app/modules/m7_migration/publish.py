"""迁移单的发布、切换与回滚。

一条批次算出来不等于可以上线：只有「账平 + 时间裁决完整 + 回放稳定」的迁移单
才允许成为当前迁移单。切换失败时指针不动，页面与导出继续读上一份完整迁移单；
已成功切换的，也可以显式回滚到栈里的上一份。
"""

from __future__ import annotations

from app import errors, store
from app.modules.m7_migration import temporal

KV_ACTIVE = "migration.active_run_id"
KV_STACK = "migration.spec_stack"


def current_run() -> dict | None:
    """当前迁移单：优先已发布的，没有发布记录时退回最新批次。"""
    run_id = store.get_kv(KV_ACTIVE)
    if run_id:
        run = store.run_by_id(str(run_id))
        if run is not None:
            return run
    return store.latest_run()


def stack() -> list[str]:
    raw = store.get_kv(KV_STACK) or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _ruling(run: dict) -> dict:
    return run.get("temporal_ruling") or temporal.build(run)


def problems(run: dict | None, replay: list[dict] | None = None) -> list[dict]:
    """发布前体检：任何一条不通过都不许切换。"""
    found: list[dict] = []
    if run is None:
        found.append({"code": "SPEC_INCOMPLETE", "detail": "没有可发布的批次"})
        return found

    ledger = run.get("ledger") or {}
    balanced = ledger.get("balanced") or {}
    if not balanced.get("all"):
        found.append({
            "code": "SPEC_INCOMPLETE",
            "detail": "账目不平，拒绝发布",
            "balanced": balanced,
        })

    ruling = _ruling(run)
    if not ruling.get("complete"):
        found.append({"code": "SPEC_INCOMPLETE", "detail": "时间口径裁决不完整，拒绝发布"})

    if replay is not None:
        checked = temporal.replay_compare(replay)
        if not checked["ok"]:
            found.append({
                "code": "TEMPORAL_REPLAY_MISMATCH",
                "detail": "回放改变了历史业务日或支付瞬间，拒绝切换",
                "changed": checked["changed"],
            })
    return found


def publish(run_id: str | None = None, replay: list[dict] | None = None) -> dict:
    """把一条完整迁移单设为当前；失败时保持原指针不变。"""
    run = store.run_by_id(str(run_id)) if run_id else store.latest_run()
    found = problems(run, replay)
    if found:
        current = current_run()
        raise errors.SpecSwitchFailed(
            "切换失败，已保留上一份完整迁移单",
            detail={
                "problems": found,
                "kept_run_id": (current or {}).get("run_id"),
                "kept_spec_available": current is not None,
            },
        )

    current_id = str(run["run_id"])
    history = stack()
    if history and history[-1] == current_id:
        changed = False
    else:
        history.append(current_id)
        store.set_kv(KV_STACK, history)
        changed = True
    store.set_kv(KV_ACTIVE, current_id)
    return {
        "published_run_id": current_id,
        "changed": changed,
        "previous_run_id": history[-2] if len(history) >= 2 else None,
        "stack": history,
        "replay": temporal.replay_compare(replay) if replay is not None else None,
    }

def rollback() -> dict:
    """回到上一份完整迁移单；栈里没有上一份时明确拒绝。"""
    history = stack()
    if len(history) < 2:
        raise errors.SpecRollbackUnavailable(
            "没有上一份完整迁移单可回退",
            detail={"stack": history, "current_run_id": history[-1] if history else None},
        )

    target_id = history[-2]
    target = store.run_by_id(str(target_id))
    found = problems(target)
    if found:
        raise errors.SpecRollbackUnavailable(
            "上一份迁移单不完整，未回退",
            detail={"target_run_id": target_id, "problems": found},
        )

    rolled_back_from = history[-1]
    history = history[:-1]
    store.set_kv(KV_STACK, history)
    store.set_kv(KV_ACTIVE, str(target_id))
    return {
        "published_run_id": str(target_id),
        "rolled_back_from": rolled_back_from,
        "stack": history,
        "run_id": (target or {}).get("run_id"),
    }


def state() -> dict:
    run = current_run()
    published = store.get_kv(KV_ACTIVE)
    return {
        "published_run_id": str(published) if published else None,
        "current_run_id": (run or {}).get("run_id"),
        "complete": bool(run and problems(run) == []),
        "stack": stack(),
        "temporal_policy": temporal.summary(run) if run else None,
    }
