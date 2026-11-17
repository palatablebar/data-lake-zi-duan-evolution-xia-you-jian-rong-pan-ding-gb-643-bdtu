"""时间字段迁移裁决：业务日与绝对瞬间分开保留，不共用一套时区口径。

两类时间字段的业务依据不同，不能用同一条「统一时区落库」规则处理：

* business_day（业务日，例如用户开户日）：依据是源端业务时区的本地日历日。
  只按 UTC 落库会把 00:00 前后的开户记录推到相邻两天；因此必须把本地墙钟
  日期作为权威业务键保留，UTC 瞬间只作审计旁证。
* instant（绝对瞬间，例如支付时刻）：依据是同一个物理时刻。审计回放要求
  支付瞬间不变，因此以 UTC 瞬时值为权威，本地时间只能由它派生用于展示。

本模块是裁决的唯一实现：迁移单页面、Markdown、CSV、运行清单都从这里取同一份
结论，避免页面与导出各说各话。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

from app import config

# 明确口径优先于任何命名猜测。键为 (表名, 列名)，均小写。
# 开户日与支付时刻是本次上游升级的两个关键字段，必须在裁决里写死。
SEMANTIC_OVERRIDES: dict[tuple[str, str], str] = {
    ("dim_user", "created_at"): config.SEMANTIC_BUSINESS_DAY,
    ("fct_pay", "paid_at"): config.SEMANTIC_INSTANT,
}

# 命名兜底：只对「时间族」字段生效，且明确无法识别时返回 None，不猜。
_BUSINESS_DAY_RE = re.compile(
    r"(^|_)(biz|business|stat|open|birth|account)(_|$)|(^|_)date($|_)|(^|_)day($|_)|"
    r"(^|_)ds($|_)|(^|_)dt($|_)|日期|开户日|业务日|账期",
    re.IGNORECASE,
)
_INSTANT_RE = re.compile(
    r"(^|_)(at|ts|time|timestamp|instant|moment)($|_)|时刻|时间戳|瞬间",
    re.IGNORECASE,
)

SEMANTIC_LABELS = {
    config.SEMANTIC_BUSINESS_DAY: "业务日",
    config.SEMANTIC_INSTANT: "绝对瞬间",
}

# 每个口径的保留依据与回放规则，页面与导出共用同一段文字。
BASIS = {
    config.SEMANTIC_BUSINESS_DAY: {
        "business": "源端业务时区的本地日历日（wall-clock date）为权威业务键",
        "audit": "另存 UTC 瞬间与源端偏移，仅供审计与追溯，不参与按日归集",
        "storage": "同时保留 business_day（本地日期）、instant_utc、source_offset",
        "replay": "回放旧结构时必须得到同一个本地日历日，禁止先转 UTC 再截日",
    },
    config.SEMANTIC_INSTANT: {
        "business": "UTC 绝对瞬间为权威业务键，同一物理时刻只有一种取值",
        "audit": "本地时间由 UTC 瞬间按显示时区派生，仅用于展示与对账标注",
        "storage": "保留 instant_utc；本地时间一律派生，不单独落库",
        "replay": "回放旧结构时必须得到同一个 UTC 瞬间，禁止按本地墙钟重新解释",
    },
}


def _base(type_raw: str | None) -> str:
    return config.base_of(type_raw or "")


def is_temporal_type(type_raw: str | None) -> bool:
    return _base(type_raw) in config.TEMPORAL_BASES


def classify(table: str, name: str) -> str:
    """给一个时间字段定业务口径；认不出来时按绝对瞬间保守处理。

    保守方向的选择理由：瞬间口径不会把相邻两天合并，误判只会让下游多一次
    重算，不会悄悄改变历史业务日。
    """
    key = ((table or "").strip().lower(), (name or "").strip().lower())
    if key in SEMANTIC_OVERRIDES:
        return SEMANTIC_OVERRIDES[key]
    token = key[1]
    if _BUSINESS_DAY_RE.search(token):
        return config.SEMANTIC_BUSINESS_DAY
    if _INSTANT_RE.search(token):
        return config.SEMANTIC_INSTANT
    return config.SEMANTIC_INSTANT


def _sort_key(field: dict) -> tuple:
    return (field.get("table") or "", field.get("name") or "", field.get("field_id") or "")


def _consumers_for(field_id: str, by_field: dict) -> list[str]:
    return sorted((by_field or {}).get(str(field_id), []) or [])


def _table_refs(consumers: list[dict]) -> dict[str, list[str]]:
    """表级引用索引：只用于页面提示「同表还有谁」，不参与必须重算的判定。"""
    out: dict[str, set[str]] = {}
    for consumer in consumers:
        for ref in consumer.get("refs") or []:
            table = ref.rpartition(".")[0].lower()
            out.setdefault(table, set()).add(consumer["name"])
    return {table: sorted(names) for table, names in out.items()}


def temporal_fields(run: dict) -> list[dict]:
    """从一次判定的差异行里挑出时间族字段，带上改名前后的名字。"""
    out: list[dict] = []
    for row in run.get("diff") or []:
        old_type = row.get("old_type")
        new_type = row.get("new_type")
        if not (is_temporal_type(old_type) or is_temporal_type(new_type)):
            continue
        name = row.get("new_name") or row.get("old_name") or ""
        out.append({
            "field_id": str(row.get("field_id") or ""),
            "table": row.get("table") or "",
            "name": name,
            "old_name": row.get("old_name"),
            "new_name": row.get("new_name"),
            "old_type": old_type,
            "new_type": new_type,
            "change": row.get("change"),
            "verdict": row.get("verdict"),
            "semantic": classify(row.get("table") or "", name),
        })
    out.sort(key=_sort_key)
    return out


def build(run: dict) -> dict:
    """生成整份时间裁决；缺任何一环都算不完整，不能发布。"""
    by_field = (run.get("resolution") or {}).get("by_field") or {}
    consumers = (run.get("refs") or {}).get("consumers") or []
    table_refs = _table_refs(consumers)
    fields: list[dict] = []
    must_recompute: list[dict] = []
    can_read: list[dict] = []

    for field in temporal_fields(run):
        semantic = field["semantic"]
        basis = BASIS[semantic]
        direct = _consumers_for(field["field_id"], by_field)
        # 业务日字段：凡直接引用本字段的下游都按日/按值消费，必须重算，
        # 否则零点附近的开户记录会跨日。
        # 瞬间字段：瞬间不变，直接引用者可继续读取；同表下游若显式按本地日
        # 截断该瞬间，才需要重算（这里不猜测，交给回放核验兜底）。
        if semantic == config.SEMANTIC_BUSINESS_DAY:
            recompute = direct
            readable: list[str] = []
        else:
            recompute = []
            readable = direct
        item = {
            **field,
            "semantic_label": SEMANTIC_LABELS[semantic],
            "business_basis": basis["business"],
            "audit_basis": basis["audit"],
            "storage_rule": basis["storage"],
            "replay_rule": basis["replay"],
            "direct_consumers": direct,
            "table_consumers": table_refs.get(field["table"].lower(), []),
            "downstream_must_recompute": recompute,
            "downstream_can_read": readable,
            "conclusion": _field_conclusion(field, basis, recompute, readable),
        }
        fields.append(item)
        if recompute:
            must_recompute.append({"field_id": field["field_id"],
                                   "field": f"{field['table']}.{field['name']}",
                                   "consumers": recompute})
        if readable:
            can_read.append({"field_id": field["field_id"],
                             "field": f"{field['table']}.{field['name']}",
                             "consumers": readable})

    business = [f for f in fields if f["semantic"] == config.SEMANTIC_BUSINESS_DAY]
    instants = [f for f in fields if f["semantic"] == config.SEMANTIC_INSTANT]
    conclusion = _overall_conclusion(business, instants, must_recompute, can_read)
    ruling = {
        "policy_version": config.TEMPORAL_POLICY_VERSION,
        "business_tz": config.BUSINESS_TZ_NAME,
        "business_offset": _offset_text(config.BUSINESS_OFFSET),
        "display_tz": config.DISPLAY_TZ_NAME,
        "fields": fields,
        "summary": {
            "fields": len(fields),
            "business_day": len(business),
            "instant": len(instants),
            "must_recompute": must_recompute,
            "can_read": can_read,
            "replay_stable": True,
        },
        "replay": replay_signature(fields),
        "conclusion": conclusion,
        # 没有时间族字段时裁决为空但完整；有字段则每个都必须定出口径。
        "complete": all(field["semantic"] in BASIS for field in fields),
    }
    return ruling


def _field_conclusion(field: dict, basis: dict, recompute: list[str],
                      readable: list[str]) -> str:
    name = f"{field['table']}.{field['name']}"
    if field["semantic"] == config.SEMANTIC_BUSINESS_DAY:
        tail = ("下游必须重算：" + "、".join(recompute)) if recompute else "当前没有登记下游，无需重算"
        return f"{name} 按业务日保留：{basis['business']}；{tail}。"
    tail = ("下游可继续读取：" + "、".join(readable)) if readable else "当前没有登记下游"
    return f"{name} 按绝对瞬间保留：{basis['business']}；{tail}。"


def _overall_conclusion(business: list[dict], instants: list[dict],
                        must_recompute: list[dict], can_read: list[dict]) -> str:
    names = lambda items: "、".join(f"{item['table']}.{item['name']}" for item in items) or "无"
    recompute = "、".join(f"{item['field']} → {'、'.join(item['consumers'])}"
                         for item in must_recompute) or "无"
    readable = "、".join(f"{item['field']} → {'、'.join(item['consumers'])}"
                        for item in can_read) or "无"
    return (
        f"业务日字段（{names(business)}）以源端业务时区本地日历日为业务依据，"
        f"另存 UTC 瞬间供审计；绝对瞬间字段（{names(instants)}）以 UTC 瞬时值为业务依据，"
        f"本地时间仅由它派生展示。必须重算：{recompute}；可继续读取：{readable}。"
        f"旧结构回放不得改变任何历史业务日或支付瞬间。"
    )


def _offset_text(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


# ---------- 回放契约 ----------

def canonical(semantic: str, value: datetime) -> str:
    """把同一时刻按口径折成可比较的业务键。

    business_day → 业务时区本地日期；instant → UTC 瞬间。
    只有口径明确，回放才可比。
    """
    if value.tzinfo is None:
        # 朴素时间：业务日按业务时区墙钟解释，瞬间按 UTC 解释
        value = value.replace(tzinfo=(timezone(config.BUSINESS_OFFSET)
                                      if semantic == config.SEMANTIC_BUSINESS_DAY
                                      else config.UTC))
    if semantic == config.SEMANTIC_BUSINESS_DAY:
        return value.astimezone(timezone(config.BUSINESS_OFFSET)).date().isoformat()
    return value.astimezone(config.UTC).isoformat()


def replay_signature(fields: list[dict]) -> dict:
    """裁决层的回放指纹：只覆盖口径与保留规则，不含易变时间。"""
    payload = [
        {"field_id": field["field_id"], "semantic": field["semantic"],
         "replay_rule": field["replay_rule"]}
        for field in sorted(fields, key=lambda item: item["field_id"])
    ]
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "signature": digest[:16],
        "stable": True,
        "fields": len(payload),
        "rule": "按 field_id 比对同一口径下的业务键；业务日比本地日期，瞬间比 UTC 瞬时值。",
    }


def replay_compare(rows: list[dict]) -> dict:
    """逐条比对旧结构回放值与新结构回放值。

    每行给出 field_id、semantic（可省，缺省按裁决）、old_value、new_value，
    值为 ISO 时间或日期。任一业务日或瞬间改变即判失败。
    """
    changed: list[dict] = []
    unchanged: list[str] = []
    for index, row in enumerate(rows or [], start=1):
        field_id = str(row.get("field_id") or f"#{index}")
        semantic = row.get("semantic") or config.SEMANTIC_INSTANT
        if semantic not in BASIS:
            semantic = config.SEMANTIC_INSTANT
        old_key = _canonical_text(semantic, row.get("old_value"))
        new_key = _canonical_text(semantic, row.get("new_value"))
        if old_key != new_key:
            changed.append({"field_id": field_id, "semantic": semantic,
                            "old": old_key, "new": new_key})
        else:
            unchanged.append(field_id)
    return {
        "ok": not changed,
        "changed": changed,
        "unchanged": unchanged,
        "checked": len(rows or []),
        "rule": "业务日比本地日期，瞬间比 UTC 瞬时值；任一改变即拒绝切换。",
    }


def _canonical_text(semantic: str, raw) -> str | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        return canonical(semantic, raw)
    text = str(raw).strip()
    if semantic == config.SEMANTIC_BUSINESS_DAY and re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        return canonical(semantic, _parse_instant(text))
    except ValueError:
        return text


def _parse_instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))


# ---------- 对外摘要 ----------

def summary(run: dict) -> dict:
    """给运行清单与批次摘要用的精简版，结论文字与页面完全一致。"""
    ruling = run.get("temporal_ruling") or build(run)
    return {
        "policy_version": ruling.get("policy_version"),
        "business_tz": ruling.get("business_tz"),
        "fields": [
            {"field_id": field["field_id"], "field": f"{field['table']}.{field['name']}",
             "semantic": field["semantic"], "semantic_label": field["semantic_label"],
             "must_recompute": field["downstream_must_recompute"],
             "can_read": field["downstream_can_read"]}
            for field in ruling.get("fields") or []
        ],
        "replay": ruling.get("replay"),
        "conclusion": ruling.get("conclusion"),
    }


def labels() -> dict:
    """口径标签，供模板与文档复用。"""
    return dict(SEMANTIC_LABELS)
