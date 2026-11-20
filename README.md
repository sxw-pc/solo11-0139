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
python -m rightscue master --db sample.sqlite --input fixtures/master-ok.json
python -m rightscue report --db sample.sqlite
```

各 `*-conflict.json` 提供重叠场次、异摘要片段和冲突份额。原始 JSON 与 SHA-256 留在 `source` 和 `source_conflict`；`decision` 引用上一决策；结算变化在 `royalty_event` 追加增减额，不覆盖历史。

## 业务状态与路径

| 子系统 | 两个主要实体 | 业务结果 |
| --- | --- | --- |
| 场次与节目版次 | `show_slot`, `program_revision` | 场地重叠冻结、节目签发版次、门禁和舞监双证开演、加演独立身份 |
| 声轨与曲目取证 | `cue_evidence`, `work_match` | 哈希同一性、作品号权威匹配、重叠片段去重、异摘要争议 |
| 权利份额与结算 | `rights_claim`, `royalty_case` | 时点有效份额、同版冲突冻结、按有效秒数和费率付款及冲销 |
| 录音母版拼接 | `master_segment`, `master_certificate` | 重叠区声样核对连续性、逐样段唯一来源、合同结论分离、冲突保留 |

取消场次会使对应声轨失效并对已结算作品产生反向事件；节目签发新版会令旧曲目匹配撤回并重算付款；同一作品声轨冲突仅冻结该作品，其他作品保持可结算。三个结果由测试分别覆盖。

母版拼接完全离线：每个原始分段登记逐秒块摘要、采样率、声道次序、设备校时（漂移与固定延迟）和交接签名。接缝必须由重叠区声样摘要证明，时间标签恰好相接不算连续；漂移同一原件只取一套速率，逐接缝各自成立但全局不相容的方案被拒绝；倒相、左右交换和固定延迟只在有校准依据时矫正。每个输出样段记录取自哪个原件的哪段采样；坏块可由重叠原件覆盖修复，缺失或迟到签名、同编号异摘要、同源重放均保留冲突并排除出方案。合同在签约时固定：`listening` 只要求整场连续，可列多条可复现原件路径；`forensic` 要求逐样段唯一来源，来源不唯一时证书为 `held` 待核。母版结论不写回版权费；场次取消、开演修正和曲目重匹配只追加新的证书版本改变适用范围，旧证书与拼接方案留在 `master_certificate` 可追溯。

本版不接入真实音频识别、票务系统或付款渠道。巡演地区权利、转授权链、退款、票房阶梯、录播许可、国际地区冲突、更名谱系和批次撤销属于后续独立扩展。
