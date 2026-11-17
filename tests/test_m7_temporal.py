"""时间口径裁决：业务日与瞬间分流、回放稳定、发布切换与回退。"""

from __future__ import annotations

import csv
import io
import json

import pytest

from app import errors, store
from app.modules.m2_types import coerce, lattice
from app.modules.m6_batches import service as batch_service
from app.modules.m7_migration import publish, temporal


def _ruling(run) -> dict:
    return run["temporal_ruling"]


def test_business_day_and_instant_are_classified_apart(run):
    ruling = _ruling(run)
    by_name = {f"{item['table']}.{item['name']}": item for item in ruling["fields"]}
    assert by_name["dim_user.created_at"]["semantic"] == "business_day"
    assert by_name["fct_pay.paid_at"]["semantic"] == "instant"
    assert ruling["summary"]["business_day"] == 1
    assert ruling["summary"]["instant"] == 1
    assert ruling["complete"] is True


def test_business_day_requires_recompute_of_direct_consumers(run):
    ruling = _ruling(run)
    created = next(f for f in ruling["fields"] if f["name"] == "created_at")
    assert created["downstream_must_recompute"] == ["dw.task_settle"]
    assert created["downstream_can_read"] == []


def test_instant_field_lets_consumers_keep_reading(run):
    ruling = _ruling(run)
    paid = next(f for f in ruling["fields"] if f["name"] == "paid_at")
    assert paid["downstream_must_recompute"] == []
    assert paid["downstream_can_read"] == []


def test_midnight_boundary_keeps_business_day_but_not_instant():
    """23:30Z 的开户记录在 +08:00 已是次日；业务日必须按本地日历日保留。"""
    moment = "2026-09-01T23:30:00+00:00"
    assert temporal.canonical("business_day", _parse(moment)) == "2026-09-02"
    assert temporal.canonical("instant", _parse(moment)) == "2026-09-01T23:30:00+00:00"

    result = temporal.replay_compare([
        {"field_id": "3003", "semantic": "business_day",
         "old_value": moment, "new_value": "2026-09-02T07:30:00+08:00"},
        {"field_id": "2004", "semantic": "instant",
         "old_value": moment, "new_value": "2026-09-02T07:30:00+08:00"},
    ])
    assert result["ok"] is True
    assert len(result["unchanged"]) == 2


def test_replay_detects_cross_day_shift():
    """只按统一时区落库，会把 23:30Z 的开户记录推到 09-01，回放必须拒绝。"""
    result = temporal.replay_compare([
        {"field_id": "3003", "semantic": "business_day",
         "old_value": "2026-09-01T23:30:00+00:00", "new_value": "2026-09-01"},
    ])
    assert result["ok"] is False
    assert result["changed"][0]["old"] == "2026-09-02"
    assert result["changed"][0]["new"] == "2026-09-01"


def test_replay_detects_instant_shift():
    result = temporal.replay_compare([
        {"field_id": "2004", "semantic": "instant",
         "old_value": "2026-09-01T23:30:00+00:00", "new_value": "2026-09-02T00:30:00+00:00"},
    ])
    assert result["ok"] is False


def test_replay_signature_is_stable_across_rerun(client):
    first = store.latest_run()
    again = batch_service.execute()
    assert _ruling(again)["replay"]["signature"] == _ruling(first)["replay"]["signature"]
    assert store.count_runs() == 1


def test_temporal_csv_matches_page_ruling(client):
    run = store.latest_run()
    response = client.get("/api/export/temporal.csv")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert {row["field"] for row in rows} == {"created_at", "paid_at"}
    by_field = {row["field"]: row for row in rows}
    assert by_field["created_at"]["semantic"] == "business_day"
    assert by_field["created_at"]["must_recompute"] == "dw.task_settle"
    assert by_field["paid_at"]["semantic"] == "instant"
    # 页面与导出同一结论
    page = client.get("/spec")
    assert run["temporal_ruling"]["conclusion"] in page.text


def test_manifest_carries_same_temporal_ruling(client):
    run = store.latest_run()
    payload = json.loads(client.get("/api/export/manifest.json").text)
    assert payload["temporal_ruling"]["conclusion"] == run["temporal_ruling"]["conclusion"]
    assert payload["temporal_ruling"]["replay"]["signature"] == \
        run["temporal_ruling"]["replay"]["signature"]


def test_timestamptz_widens_from_timestamp():
    verdict = lattice.compare(coerce.parse_type("timestamp"), coerce.parse_type("timestamptz"))
    assert verdict["change"] == "widen"


# ---------- 发布、切换与回退 ----------

def test_publish_then_rollback_restores_previous_spec(client):
    first_run = store.latest_run()
    assert publish.publish()["published_run_id"] == first_run["run_id"]

    # 再跑一条不同批次并发布，回退栈压入两份
    second_run = batch_service.execute(params={"wave": "second"})
    published = publish.publish(second_run["run_id"])
    assert published["published_run_id"] == second_run["run_id"]
    assert publish.state()["stack"] == [first_run["run_id"], second_run["run_id"]]

    rolled = publish.rollback()
    assert rolled["published_run_id"] == first_run["run_id"]
    assert rolled["rolled_back_from"] == second_run["run_id"]
    assert publish.current_run()["run_id"] == first_run["run_id"]
    # 页面与导出都回到上一份完整迁移单
    assert json.loads(client.get("/api/export/manifest.json").text)["run_id"] == first_run["run_id"]


def test_failed_switch_keeps_previous_complete_spec(client):
    run = store.latest_run()
    publish.publish()
    with pytest.raises(errors.SpecSwitchFailed) as caught:
        publish.publish(replay=[{
            "field_id": "3003", "semantic": "business_day",
            "old_value": "2026-09-01T23:30:00+00:00", "new_value": "2026-09-01",
        }])
    detail = caught.value.detail
    assert detail["kept_run_id"] == run["run_id"]
    assert detail["kept_spec_available"] is True
    assert publish.current_run()["run_id"] == run["run_id"]


def test_publish_rejects_unbalanced_run(client):
    run = store.latest_run()
    broken = json.loads(json.dumps(run))
    broken["ledger"]["balanced"] = {"all": False}
    broken["batch_key"] = "test-unbalanced"
    broken["run_id"] = "fm-unbalanced"
    broken["version_seq"] = run["version_seq"] + 1
    store.save_run(broken)
    with pytest.raises(errors.SpecSwitchFailed) as caught:
        publish.publish("fm-unbalanced")
    assert caught.value.detail["problems"][0]["code"] == "SPEC_INCOMPLETE"


def test_rollback_without_previous_refuses(client):
    publish.publish()
    with pytest.raises(errors.SpecRollbackUnavailable):
        publish.rollback()


def test_exports_read_published_spec(client):
    run = store.latest_run()
    publish.publish()
    # 新批次不自动夺走「当前迁移单」指针
    batch_service.execute(params={"wave": "third"})
    assert publish.current_run()["run_id"] == run["run_id"]
    manifest = json.loads(client.get("/api/export/manifest.json").text)
    assert manifest["run_id"] == run["run_id"]


def test_rerun_same_batch_does_not_grow_rows(client):
    before = store.count_runs()
    batch_service.execute()
    batch_service.execute()
    assert store.count_runs() == before


def _parse(text: str):
    from datetime import datetime

    return datetime.fromisoformat(text)
