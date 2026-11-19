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

## 增强起点：道具装箱条码

先导入 `show-ok.json`，再运行 `python -m rightscue props --db sample.sqlite --input fixtures/props-ok.json`，可把当前节目单需要的道具序号与装箱条码逐项对照，缺少序号时报告 `incomplete`。当前只按条码字符串存在与否判断，不核对实物封签、拆箱后子件、同件重复占用或签审过的等效替代品。
