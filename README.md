# 儿童口腔长期观察

服务于儿童牙齿与颌骨发育的**连续随访与共同决策**后端。`domain.json` 是临床沟通的基础词表（发育阶段、风险因素、建议类型），`rules.py` 是带版本的临床规则，`store.py` 是追加式 SQLite 存储，`service.py` 提供 HTTP API 与授权控制。

- 自检：`python3 service.py --check`（校验词表与规则一致，打印冻结的规则版本哈希）
- 测试：`python3 -m unittest -v`
- 启动：`python3 service.py [--port 8000] [--db data/pedo.db]`，健康检查 `GET /health`

## 临床安全设计

1. **年龄不能单独给出矫治结论。** 规则引擎只依据传入证据评估；年龄仅用于发育阶段定位与复诊节奏。即使规则命中早治候选，"开始干预"还要求三项硬性证据齐备：重度错𬌗的临床记录、头影测量影像摘要、正畸专科第二意见；缺一项就降级为"专科会诊/补充检查"，并在 `missing_evidence` 中列明。
2. **规则输出解释而非黑箱结论。** 每个议题返回 `advice`、`reason`（为何观察/补充/转专科）、`missing_evidence`（还缺哪些证据）、`triggered_rule`。
3. **复诊只追加，不覆盖。** 每次检查有递增 `seq`；趋势端点按指标汇总历次取值，变化是比较出来的。
4. **错误记录只能更正、不能删除。** 更正写入新行、旧行标记作废并在 `corrections` 留痕（旧值/新值/原因/更正人），可沿更正链回溯。建议发出时保存**证据快照**，事后更正不会篡改历史建议。
5. **不同问题 = 独立但可关联的计划。** 龋病、持续吮指、口呼吸疑点、严重咬合异常、清洁不足各自建计划，可用 `/plans/{id}/link` 关联。
6. **第二位医生意见与家长选择都保留**（`opinions`、`decisions`），支持共同决策。
7. **授权到期即失效。** 所有患者数据访问都需要当前有效（未撤销、未过期）的授权；写临床记录要求 `clinician` 角色，`guardian` 只能读和登记家长选择。
8. **逾期随访不泄露给无关人员。** `GET /followups/overdue` 只在调用者被授权的患者范围内返回。
9. **可追溯。** `GET /advice/{id}/trace` 从一次建议追溯到检查、当时证据快照与规则版本快照（含 sha256）。

## 议题与建议类型

五个独立议题（`issue`）：`caries`（龋病进展）、`severe_malocclusion`（严重咬合异常）、`finger_habit`（持续吮指）、`mouth_breathing`（口呼吸疑点）、`poor_hygiene`（清洁能力不足）。

建议类型：`日常护理`、`继续观察`、`补充检查`、`专科会诊`、`开始干预`。观察指标目录见 `rules.OBSERVATION_INDICATORS`（11 项），影像只保存摘要：`panoramic`、`lateral_cephalometric`、`intraoral_photos`、`bitewing`。

## HTTP 接口

所有请求带 `X-User-Id` 头（`/health` 除外），请求/响应均为 UTF-8 JSON。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/patients` | 建档 |
| GET | `/patients/{pid}` | 患者信息（需授权） |
| POST/GET | `/patients/{pid}/consents` | 登记授权 / 查看授权列表（行政动作，认证即可） |
| POST | `/patients/{pid}/consents/{cid}/revoke` | 撤销授权 |
| POST/GET | `/patients/{pid}/exams` | 追加一次检查（含 findings/images/home_care）/ 列出历次检查 |
| GET | `/exams/{id}` | 检查详情（含已更正项标注） |
| POST | `/exams/{id}/evaluate` | 运行规则，返回各议题建议、缺失证据、复诊间隔 |
| POST | `/exams/{id}/advice` | 在评估基础上发出建议（固化证据快照与规则版本） |
| GET | `/advice/{id}` / `/advice/{id}/trace` | 建议详情 / 追溯到检查+证据+规则版本 |
| POST | `/patients/{pid}/items/{findings\|images\|home_care}/{id}/correction` | 更正错误记录（必须填 reason、updates） |
| GET | `/items/{kind}/{id}/chain` | 查看更正链 |
| GET | `/patients/{pid}/corrections` | 患者全部更正记录 |
| GET | `/patients/{pid}/trends/{metric}` | 某指标跨复诊趋势 |
| GET | `/patients/{pid}/timeline` | 纵向时间线聚合 |
| POST/GET | `/patients/{pid}/plans` | 建计划（按 issue）/ 列计划 |
| POST | `/plans/{id}/link` / `/close` | 关联计划 / 写明结局关闭 |
| POST | `/plans/{id}/followups` | 安排复诊（due_date） |
| POST | `/followups/{id}/complete` | 用一次真实检查完成复诊 |
| GET | `/followups/overdue` | 逾期清单（仅限本人被授权患者） |
| POST/GET | `/patients/{pid}/opinions` | 第二意见 |
| POST/GET | `/patients/{pid}/decisions` | 家长选择（guardian 可写） |

### 典型流程

```
建档 → 登记 clinician/guardian 授权（含 expires_at）
     → 追加检查（阶段、月龄、观察指标、影像摘要、家庭护理执行）
     → evaluate：看建议与缺失证据 → 必要时补检查/请第二意见
     → advice：固化建议 → 按议题建独立计划、安排复诊
     → 复诊：再建一次检查，trends/timeline 比较变化
     → 记录第二意见与家长选择；记录错误用 correction 更正
```

## 规则版本管理

规则版本号在 `rules.RULE_VERSION`；发布时 `rule_snapshot()` 冻结词表、指标目录、硬性证据门槛并计算 sha256，启动时写入 `rule_versions` 表（`INSERT OR IGNORE`，已存在版本不变）。修改规则应升版本号，使新旧建议各自可忠实复现。
