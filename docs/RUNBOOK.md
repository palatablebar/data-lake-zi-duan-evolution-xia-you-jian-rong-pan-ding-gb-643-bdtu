# 运行手册 fieldmap

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18202/api/health
```

宿主端口 `18202`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18202/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `fieldmap-data` 挂着 `/data/fieldmap.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/temporal.csv` | 时间裁决 CSV |
| GET | `/api/export/manifest.json` | 运行清单（含时间裁决） |
| GET | `/api/temporal` | 当前时间裁决与发布状态 |
| POST | `/api/temporal/replay` | 逐条回放核验（业务日比本地日期，瞬间比 UTC） |
| GET | `/api/spec/state` | 当前 / 已发布迁移单与回退栈 |
| POST | `/api/spec/publish` | 发布当前迁移单；可带 `replay` 数组先验回放 |
| POST | `/api/spec/rollback` | 回退到上一份完整迁移单 |

## 6 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次 | 先在 `/batches` 点「跑一次」 |
| 409 `SPEC_SWITCH_FAILED` | 账不平、裁决不完整或回放改变业务日/瞬间 | 看 `detail.problems`；原迁移单保持不变 |
| 409 `SPEC_ROLLBACK_UNAVAILABLE` | 回退栈里没有上一份完整迁移单 | 确认已发布过至少两份，或先修复当前迁移单 |

### 6.1 时间口径处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 日报零点附近多/少一天 | 开户日被统一时区落库后再截日 | 按 `business_day` 保留源端本地日历日，重算直接引用者 |
| 审计回放区间错位 | 支付时刻被按本地墙钟重新解释 | 按 `instant` 以 UTC 为权威，本地时间改为派生 |
| 重跑行数变化 | 批次键被参数或时区影响 | 检查 `params` 与生效区间；同输入应命中 `RUN_REUSED` |

## 7 端口与并发

同一批其他项目占用 18200-18209；本服务固定 18202。同时起多个栈时用批目录里的 `scripts/slot.sh` 限流。
