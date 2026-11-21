# 演出版权使用采录台

增强素材包含离线座席视线核证入口：先登记 show（节目签发版次可带 `scenes` 场景清单），再运行 `python -m rightscue sightline --db sample.sqlite --input fixtures/sightline-geometry.json`。该入口采录有来源的三维座席眼位范围、有面积的舞台目标边界、带误差带的移动遮挡物（板、幕布、临时摄影机）与事先售出的视线合同，逐席给出 `sellable` / `reseat_required` / `resurvey` / `invalidated` 结论；仅含人工 `visual_clear` 的旧问卷（`sightline-ok.json`）仍可录入，但只作人工记录，不参与几何核证。

## 视线核证规则

- 目标是有面积的平面区域，核对边界顶点与各边中点，不能只打中心点。
- 遮挡物实测位置带误差范围，射线须清除整个误差盒；边界等号的擦碰不算碰撞。
- 一个连续场景内眼位是同一可持续姿态：单轴眼位用解析法求可行高度区间并跨场景求交，三维眼位用确定性网格回退；不得为每个目标边缘另选有利眼位。
- 场景间能否调整姿态以售前合同为准：`fixed` 要求所有场景存在共同眼位解，`adjustable` 允许每场各取一个可持续眼位。发现遮挡不会降级已售合同。
- 同一遮挡物同时段的互不相容测绘（误差盒不相交）标 `conflict` 并对相关席位判 `resurvey`，两份原始测绘都保留，不以最后上传覆盖。
- 节目改版、换场馆或取消会令原适售结论失效（`superseded` / `invalidated`），原测绘、适售版次和已发换席单保留；节目改版后按新场景重新核证。
- 换席分配保证一个替代席不被两张票同时占用；无同合同空席时换席单判 `failed` 并写出冲突票号。

本地合成的剧场场次、声轨使用证据和作品权利声明经三个独立入口采录。它为版权结算员提供逐作品付款与争议结果；运行中不联网、不含真实作品或权利人。座席核证与版权结算完全隔离，不写入任何 royalty 表。

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
