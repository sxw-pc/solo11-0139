# 演出版权使用采录台

本地合成的剧场场次、声轨使用证据和作品权利声明经三个独立入口采录。它为版权结算员提供逐作品付款与争议结果；运行中不联网、不含真实作品或权利人。

## 快速运行

需要 Python 3.11+，在仓库根目录执行：

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m rightscue demo --db demo.sqlite
python -m pytest -q
```

每个子系统可独立运行，`report` 从 SQLite 读取证据和决策链：

```powershell
python -m rightscue show --db sample.sqlite --input fixtures/show-ok.json
python -m rightscue cue --db sample.sqlite --input fixtures/cue-ok.json
python -m rightscue rights --db sample.sqlite --input fixtures/rights-ok.json
python -m rightscue report --db sample.sqlite
```

各 `*-conflict.json` 提供重叠场次、异摘要片段和冲突份额。原始 JSON 与 SHA-256 留在 `source` 和 `source_conflict`；`decision` 引用上一决策；结算变化在 `royalty_event` 追加增减额，不覆盖历史。

## 业务状态与路径

| 子系统 | 两个主要实体 | 业务结果 |
| --- | --- | --- |
| 场次与节目版次 | `show_slot`, `program_revision` | 场地重叠冻结、节目签发版次、门禁和舞监双证开演、加演独立身份 |
| 声轨与曲目取证 | `cue_evidence`, `work_match` | 哈希同一性、作品号权威匹配、重叠片段去重、异摘要争议 |
| 权利份额与结算 | `rights_claim`, `royalty_case` | 时点有效份额、同版冲突冻结、按有效秒数和费率付款及冲销 |

取消场次会使对应声轨失效并对已结算作品产生反向事件；节目签发新版会令旧曲目匹配撤回并重算付款；同一作品声轨冲突仅冻结该作品，其他作品保持可结算。三个结果由测试分别覆盖。

本版不接入真实音频识别、票务系统或付款渠道。巡演地区权利、转授权链、退款、票房阶梯、录播许可、国际地区冲突、更名谱系和批次撤销属于后续独立扩展。

## 道具包放行：装箱、异地拆箱、子件与等效替代

装箱条码只证明交运时箱里装过什么。放行按**实物序号与功能范围**判定，证据分五类，都经 `kind` 区分并可重放：

| `kind` | 关键字段 | 作用 |
| --- | --- | --- |
| `commission` | `commission_id`, `scope`, `roles`, `signed_at`, 双签 | 装箱前确定本场委托覆盖哪些角色（如纯音效、录播近景） |
| `packing` | `case_id`, `seal`, `packed_at`, `requirements`, `units`, `components`, `scans` | 封箱单：装入的整套道具、其可拆子件与箱外条码 |
| `unpack` | `id`, `case_id`, `venue`, `seal_intact`, 双签, `items` | 异地开箱双签与实物清点；只有 `status='sealed'` 的拆箱进入可用库存 |
| `substitution` | `cert_id`, `original_serial`, `substitute_serial`, `role`, `capabilities`, `valid_shows`, 双签 | 等效替代证书：只证明所列性能项目，并按场次生效 |
| `return` | `id`, `returned_at`, 双签, `items(present)` | 反向归还清点；旧装箱单不能覆盖异地丢失 |

放行结果写在 `prop_release`（追加式）及逐角色 `prop_release_role`。`report.build(db)["prop_release_summary"]` 列出每个角色所用的原始序号与实物序号、证据路径（证书 → 拆箱 → 子件）、证书覆盖到的具体性能、仍未满足的子要求以及冲突来源，而不只是一个 ready/incomplete 标记。

判定规则：

* 箱外条码、封箱单和拆箱双签缺一不可；缺箱子清单或封签破损时整场 `hold`，不会因为条码存在而放行。
* 同一序号化子件只能装在一套道具上。两套同时上场的道具都申请同一电机 M 时，按开场时间（同刻则按场次号）只有一套占用；另一套明确列出被占用的 `motor-M` 和仍缺的件。
* 同一箱体条码重复出现只生成一套子件，重复拆箱记为 `replay_conflict`；同一 `source_id` 同字节重放返回 `duplicate`，不新增决策。
* 替代证书按性能项目逐项覆盖：`C4_tone` 不能替代 `visible_clock`。外观相似或名称相同不自动等效；证书上的 `role` 只是说明用途，覆盖与否看 `capabilities`。
* 委托在装箱前确定；装箱后不能加宽角色范围，也不能以更宽的合同覆盖缺件。
* 节目签发新版、改场馆、取消场次都会追加新的道具决定并令旧 `ready` 失效；返还清点发现装箱有、拆箱无的件会记入 `prop_loss`。
* 道具决定与 `cue` / `rights` 完全分离，声轨匹配和版权费金额不受道具条码影响。

验收场景可直接运行：

```powershell
python -m rightscue show --db sample.sqlite --input fixtures/show-ok.json
python -m rightscue props --db sample.sqlite --input fixtures/props-commission-sound.json
python -m rightscue props --db sample.sqlite --input fixtures/props-pack.json
python -m rightscue props --db sample.sqlite --input fixtures/props-unpack.json
python -m rightscue props --db sample.sqlite --input fixtures/props-cert-sound-only.json
python -m rightscue report --db sample.sqlite
```

`bell-7` 在拆箱缺失、`bell-9` 仅持 C 音证书时：纯音效委托 `props-commission-sound.json` 放行（`substituted`），录播近景委托 `props-commission-open.json` 保持 `hold`，直到补 `props-cert-appearance.json` 或找回 `bell-7`。

### 兼容旧装箱单

不含 `kind`、只有 `requirements` + `scans` 的旧载荷仍按条码集合记 `ready/incomplete`（`prop_case`），便于回放既有数据；它不参与新版放行判断，也不改变声轨与版权费。

