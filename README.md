# 儿童口腔长期观察

服务于儿童牙齿和颌骨发育的**连续随访与共同决策**后端：按发育阶段、观察指标、
风险因素与建议类型保存结构化检查、影像摘要、家庭护理执行情况、二诊意见与家长选择。

## 设计底线（临床安全）

1. **不能用单一年龄自动给出矫治结论**。年龄/发育阶段只决定观察节奏；
   没有当次结构化检查时，规则只能输出「补充检查」并列出缺哪些证据。
2. **规则输出必须解释"为什么观察、为什么转专科、还缺什么证据"**
   （`rationale` / `missing_evidence` / `gate_note`）。
3. **正畸类「开始干预」必须通过专科闸门**：严重咬合指征 + 规定影像齐备 +
   正畸专科二诊结论为「开始干预」，三者缺一只能止步于补充检查/专科会诊。
4. **旧记录永不被覆盖**：检查、影像摘要、家庭护理均只追加；修订产生新版本行并经
   `supersedes_id` 串成更正链（含修订原因、修订人、时间）。
5. **复诊比较趋势而不是覆盖**：`/trends` 对历次检查的同一指标算改善/稳定/加重/数据不足。
6. **第二位医生意见与家长选择只追加保留**，随时间线长期可见。
7. **不同问题相互独立但可关联**：持续吮指、口呼吸疑点、龋病进展各有独立 problem/plan，
   评估时按规则登记"可能相关"链接，也可手动关联，计划不合并。
8. **授权到期/吊销即断访**；逾期随访清单只暴露给对该患者有有效授权的人员，
   且仅返回最小字段（不附姓名、不附病情细节）。
9. **任何建议都可溯源**：建议落库时固化规则版本、规则校验和与证据快照；
   溯源端点会标明引用的检查记录当前是否仍有效、被哪条更正替代。

## 运行

```bash
pip install -r requirements.txt
python3 service.py --check     # 词表/存储/规则自检
python3 service.py --port 8000 # 启动（SQLite 默认 followup.db）
python3 -m pytest              # 28 个测试
```

健康检查：`GET /health` → `{"status":"ok","service":"pediatric-oral-followup"}`

## 文件

| 文件 | 职责 |
|---|---|
| `domain.json` | 受控词表：发育阶段、风险因素、建议类型、观察指标（类型/取值/高值是否风险）、影像类型、家庭护理项、授权范围→可见风险映射等 |
| `rules.py` | 纯函数规则引擎：五条风险规则、阈值规则本 `RULEBOOK`、`RULE_VERSION` 与 `rulebook_checksum()`、专科闸门、随访间隔 |
| `storage.py` | 只追加 SQLite：版本化记录、更正链、问题/计划/建议/随访、溯源 |
| `app.py` | HTTP 路由、词表校验、授权范围过滤、证据包组装、趋势比较、评估落库 |
| `service.py` | 启动入口与 `--check` |

## 授权模型

请求带 `X-User-Id` 头。授权按患者 × 用户 × 范围授予，带到期时间，可吊销。

- `儿童口腔随访`：全风险可见、可写临床记录；
- `龋病管理`：仅龋病进展/清洁能力不足；
- `正畸评估`：仅严重咬合异常/持续吮指/口呼吸疑点；
- `影像查阅`：影像摘要读写。

响应里的指标、建议、问题、逾期项均按"范围→风险"映射过滤；对不可见资源返回 404
而非 403，避免泄露存在性。到期与吊销的授权统一表现为"无有效授权"。

## 主要端点

```
POST   /users
POST   /patients
POST   /patients/{pid}/consents                 # 授权（范围+到期）
DELETE /consents/{cid}                          # 吊销
POST   /patients/{pid}/visits                   # 就诊（发育阶段）
POST   /visits/{vid}/exams                      # 结构化检查（受控指标）
POST   /visits/{vid}/imaging                    # 影像摘要
POST   /visits/{vid}/home-care                  # 家庭护理执行情况
POST   /visits/{vid}/second-opinions            # 第二位医生意见（结论取自建议类型）
POST   /visits/{vid}/parent-decisions           # 家长选择
POST   /visits/{vid}/evaluate[?persist=true]    # 规则评估（预览/落库）
GET    /patients/{pid}/timeline                 # 按就诊聚合的完整时间线
GET    /patients/{pid}/trends?metrics=前牙覆盖  # 跨次趋势（改善/稳定/加重/数据不足）
GET    /patients/{pid}/problems                 # 问题、独立计划与关联链接
POST   /patients/{pid}/problem-links            # 手动关联两个问题
POST   /patients/{pid}/problems/{id}/close      # 关闭问题（连带完成其活跃计划）
GET    /patients/{pid}/advice                   # 历次建议（不覆盖）
GET    /advice/{aid}/trace                      # 建议→规则版本+证据有效性溯源
POST   /records/{exam|imaging|home_care}/{id}/correct  # 修订（必须给原因，产生更正链）
GET    /records/{kind}/{id}/chain               # 查看更正链
GET    /patients/{pid}/follow-ups?today=YYYY-MM-DD     # 患者随访（含逾期计算）
GET    /follow-ups/overdue?today=YYYY-MM-DD     # 跨患者逾期（最小字段，按授权过滤）
POST   /follow-ups/{fid}/complete               # 复诊完成
POST   /follow-ups/{fid}/cancel
```

`evaluate`（不落库预览）允许在证据不足时查看规则会输出什么；`persist=true`
要求当次就诊已有结构化检查，否则拒绝——系统不会仅凭年龄/发育阶段建立矫治相关计划。

## 规则行为摘要（版本 1.0.0）

- **严重咬合异常**：无检查→补充检查；重度覆盖(≥6mm)/多数前牙或后牙反𬌗/重度拥挤→
  先补全景片+头颅侧位片，再经正畸专科闸门；个别前牙反𬌗属可早评估情形，
  直接转正畸专科但仍不绕过闸门；中度→继续观察趋势。
- **持续吮指**：乳牙列期以家庭戒除引导为主；混合牙列期睡眠中持续≥12 个月转正畸专科。
- **口呼吸疑点**：白天持续或双侧鼻阻→耳鼻喉科会诊；夜间可疑→家庭观察记录后复查。
  该规则永远不直接产出正畸干预。
- **龋病进展**：多发活动龋(≥3 牙面)或趋势加重→儿童牙科；少量活动龋→补充检查/咬翼片。
- **清洁能力不足**：高菌斑指数或中指数+需家长协助/执行不足→强化家庭护理指导。

阈值集中在 `rules.py` 的 `RULEBOOK`；改动阈值会改变校验和，历史建议仍保留其产生时的
版本号与校验和，因此"这条建议是哪个版本的规则、基于哪些记录"始终可查。
