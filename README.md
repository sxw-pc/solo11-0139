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

## 离线扩声回路测量与增益许可

扩声许可与版权使用、录音母版结论完全分开：它只回答某场演出事先签定的接线方案在测量证据下能否放行，不写入任何 `royalty_case` 或母带结论。

```powershell
python -m rightscue pa --db sample.sqlite --input fixtures/pa-contract.json
python -m rightscue pa --db sample.sqlite --input fixtures/pa-calibration.json
```

每次声场校准（`pa` 入口，`kind=calibration`）记录哪组扬声器发出哪条独立测试脉冲、哪支麦克风收到的逐样响应、采样率、场馆布点（`layout`）和接线矩阵。全部脉冲组成多输入多输出有限冲激响应（MIMO FIR，表 `sound_fir`）；逐支探针的 RMS 只用于 `audio_health` 设备健康，不参与回路判断。同一扬声器上同源脉冲的复制只标记为 `duplicate`，不能冒充第二次独立测量；削顶按逐样标记（`sound_response_clip`），只给出有依据的响应下界；缺段只允许落在已观测幅度界内的响应范围。

接线合同（`kind=contract`）必须在测量前签发，固定实际启用通道与每通道合同增益范围；设备增益/白平衡校准（`kind=gain_calibration`）是一场所有通道共同生效的一个版本，公共误差不会逐条回授边挑选最有利值。对启用通道及整个增益超空间（含公共校准误差的两个极端方向），许可检验闭环延迟系统状态矩阵的谱半径（增长模态幅值）和扬声器峰值上限，并给出稳定边界、频段共振和可复核的回授环路：

- 所有可接受解释都稳定且峰值有余量 → `approved`，给出可用增益范围；
- 干净证据下最坏角已达/超过一 → `rejected`，给出不稳定模式与测量依据；
- 证据带同时含稳定与不稳定解释（削顶/缺段）→ `inconclusive`，合同待补测，不得宣称安全。

场馆更正、接线换版、校准换版或场次取消都会令旧许可置 `superseded=1` 失效，但原始脉冲、FIR 和旧版许可仍保留在报告中。发现啸叫后倒签单路合同无法覆盖双路演出：测量后签发的合同直接拒绝；同一份脉冲与场馆证据对不同的真实接线合同给出不同放行结果。

本版不接入真实音频识别、票务系统或付款渠道。巡演地区权利、转授权链、退款、票房阶梯、录播许可、国际地区冲突、更名谱系和批次撤销属于后续独立扩展。
