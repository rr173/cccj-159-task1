# ECO 签核基线服务 (eco-baseline-service)

芯片设计工程变更(ECO)与签核基线服务。设计团队提交 ECO 时冻结**网表、约束、
受影响模块、验证矩阵**;系统按**依赖图**触发时序(timing)、功耗(power)、
形式验证(formal)、版图(layout)检查;只有全部必需检查**针对同一基线**
通过、且**双角色签核**完成后,才能形成新的发布基线。

纯 Python 标准库实现,零第三方依赖(SQLite 持久化 + `http.server` REST API)。

## 核心不变量

1. **结果绑定四元组**:每个检查结果绑定
   `(eco_id, snapshot_hash, base_baseline_id, constraint_epoch)`。
   只有与 ECO 当前绑定完全一致的 `PASSED` 结果才计入签核就绪与发布判定。
2. **旧结果不得拼接复用**:ECO 修订(快照变化)、基础约束更新(约束纪元推进)、
   其他 ECO 发布(头基线推进)都会使在途检查运行失效、已完成的签核撤销;
   **迟到的结果一律被拒绝**并写入审计日志(`stale_result_rejected`)。
3. **冲突显式裁决**:两个 ECO 修改重叠模块时,后到者自动进入 `BLOCKED` 排队,
   生成冲突记录,必须显式裁决:`queue`(排队)/ `merge`(并入先到者)/
   `partition`(拆分模块集)/ `reject`(驳回)。
4. **发布原子性**:发布两段式执行(意向日志 + 单事务提交)。进程在任意点崩溃,
   `recover()` 都能回滚到发布前状态,绝不产生半成品基线;恢复后可安全重试。
5. **双角色签核**:`DESIGN_AUTHORITY` 与 `VERIFICATION_AUTHORITY` 缺一不可,
   且必须由不同人完成;签核绑定 ECO 快照,快照变化即自动撤销。

## 检查依赖图

默认 DAG(可在 Engine 构造时替换):

```
timing ──┬──> power
         └──> layout
formal(独立)
```

冻结时创建全部必需检查运行;无依赖者立即 `DISPATCHED`(携带一次性派发 token),
其余 `PENDING`,依赖全部通过后才被触发。

## ECO 状态机

```
DRAFT --freeze--> FROZEN --全绿--> READY_FOR_SIGNOFF --签1--> SIGNOFF_PARTIAL
  ^                 |                                    --签2--> SIGNOFF_COMPLETE
  |                 |--冲突--> BLOCKED(排队)                        |
  |--修订/失效-------+                                                v
  +------------------------- 级联失效 ----------------------- RELEASING --> RELEASED
```

## 目录结构

```
eco_service/
  domain.py     # 状态枚举、快照哈希、基线合成(纯函数)
  store.py      # SQLite 持久化(事务、审计事件表)
  engine.py     # 编排核心:冻结/调度/结果回收/签核/发布/恢复/冲突裁决
  diff.py       # 基线差异包(JSON + zip 导出)
  workbench.py  # 签核工作台聚合视图
  api.py        # REST API(stdlib http.server)
tests/test_service.py   # 19 个场景测试
demo.py                 # 端到端演示
```

## 运行

```bash
cd eco_baseline_service
python3 -m unittest discover -s tests -v   # 测试
python3 demo.py                            # 端到端演示
python3 -m eco_service.api --db eco.db --port 8080   # 启动 HTTP 服务
```

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/baselines/seed` | 建立初始基线 |
| GET | `/baselines` / `/baselines/{id}` | 基线查询 |
| GET | `/baselines/{a}/diff/{b}` | 基线差异(JSON) |
| POST | `/baselines/{a}/diff/{b}/package` | 导出差异包 zip |
| POST | `/constraints/update` | 基础约束更新(推进约束纪元,在途结果全部失效) |
| POST | `/ecos` | 创建 ECO(changes / affected_modules / matrix) |
| POST | `/ecos/{id}/freeze` | 冻结快照并按依赖图触发检查 |
| POST | `/ecos/{id}/amend` | 修订(旧检查与签核失效,回到 DRAFT) |
| POST | `/ecos/{id}/withdraw` | 撤回(释放排队者) |
| POST | `/ecos/{id}/checks/{run}/results` | 回传检查结果(需携带派发 token) |
| POST | `/ecos/{id}/signoffs` | 角色签核 `{role, user}` |
| POST | `/ecos/{id}/release` | 发布新基线(前置校验全部不变量) |
| GET | `/ecos/{id}/readiness` | 就绪状态(检查矩阵 + 签核进度) |
| GET | `/conflicts` / POST `/conflicts/{id}/adjudicate` | 冲突查询与裁决 |
| GET | `/workbench/pending` | 签核工作台待办 |
| GET | `/workbench/ecos/{id}` | 单 ECO 仪表盘(含失效原因、近期事件) |
| GET | `/events?entity=` | 审计日志 |
| POST | `/admin/recover` | 崩溃恢复(回滚未完成发布) |

## 关键场景语义

- **验证结果迟到**:结果必须携带当前派发 token;运行已失效 / token 不符 /
  绑定四元组不符 / ECO 状态不接收,一律拒绝并记审计事件。
- **基础约束更新**:`POST /constraints/update` 推进约束纪元并生成
  `CONSTRAINT_UPDATE` 基线;所有在途 ECO 的检查与签核失效、重置为 DRAFT,
  需在新纪元上重新冻结验证。
- **重叠模块**:冻结时检测与所有在途(含已排队)ECO 的模块交集,先到者持有
  模块所有权;`merge` 裁决把后到者变更并入先到者(同模块以后到者为准并留痕)。
- **发布中断**:阶段一(INTENT 日志)与阶段二(单事务建基线)之间或之中崩溃,
  `recover()` 将 ECO 复位到 `SIGNOFF_COMPLETE`,可安全重试;阶段二内部崩溃
  由 SQLite 事务保证整体回滚。
- **基线差异包**:模块增删改(netlist hash 对比)、约束键级差异、验证矩阵差异,
  以及两个基线之间全部发布 ECO 的签核记录与检查证据(含绑定四元组),导出为
  `diff_package.json` + `manifest.json` 的 zip。
