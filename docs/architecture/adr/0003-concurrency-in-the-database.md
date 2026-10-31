# ADR 0003: 并发正确性放在数据库——部分唯一索引、锁序与"超卖零容忍"

日期: 2026-10-31 · 状态: 已接受
关联: 产品 spec §8.2/§8.3/§31.4/§32 · backend/app/modules/tasks/claim_service.py ·
backend/app/modules/tasks/models.py（`uq_assignment_claims_active_assignment`、
`uq_assignment_claims_active_user_task`）·
backend/app/modules/submissions/upload_service.py（LOCK ORDER 契约）·
docs/loadtest/capacity-report-2026-10.md（S2 实证）

## 决策

名额与状态类的并发不变量**由 PostgreSQL 的约束与行锁保证**，应用层检查
只是第一道友好的失败路径，绝不是最后一道：

1. **两把部分唯一索引是"超卖"的最终防线**：
   - `uq_assignment_claims_active_assignment`（WHERE claim 处于活跃态）：
     一个任务单元同时至多一个活跃领取；
   - `uq_assignment_claims_active_user_task`（同 WHERE）：同一用户在同一
     任务上至多一个活跃领取。
   部分索引让"历史可保留、活跃唯一"一个索引同时表达两种语义——放弃
   领取/过期后行回到可领池，历史行不碍事。
2. **领取路径的锁序契约**（claim_service，spec §8.3）：
   `users 行 FOR UPDATE（同人串行化）→ task FOR SHARE → 随机候选
   FOR UPDATE SKIP LOCKED → 快照 + 占用同一事务`。SKIP LOCKED 使
   高并发抢名额时各请求天然错开不同单元，而不是全体排队在同一个热门行上。
3. **写路径统一锁序文档化**：upload_service 模块头声明
   `user → claim → intent` 的获取顺序，create/finalize/验证回边全部遵守；
   Celery 侧的状态迁移也按同一顺序拿锁（reward_lock_service 先 claim
   `FOR UPDATE` 再写状态，`_restore_actionable_claim` 同构）。
4. **时钟采样契约**：`claimed_at` 等业务时刻在**全部锁取得之后、第一条
   消费它的规则之前**采样（claim_service 的 CLOCK SAMPLING CONTRACT），
   锁等待不会向后扭曲截止比较与 RELATIVE 期限锚点。

## 理由

- 应用层"先查后写"（COUNT-then-INSERT）在任何交错下都可错；数据库约束
  在任何交错下都不会错。Python 检查失败是 bug，约束失败是被正确拒绝。
- 容量战役给了实证（capacity-report S2）：**300 人并发抢 60 个名额，恰好
  60 胜、零超发、240 个落败者全部拿到 §8.4 稳定业务码**；S1 的 §32
  幂等（重放同一 intent 返回同一 submission）同样由
  `UNIQUE(claim_id, version)` + intent 单次消费列在库上锚定。
- 正确性放数据库使压测检查器成为可能：负载后直接查库断言
  "零丢失/零重复/零超发"（loadtest/check_s1.py、check_s2.py）——如果
  保证分散在应用层，这类断言无从写起。

## 被否决的替代

- **Redis/分布式锁做名额**：引入第二事实源与租约语义，且失去事务内
  与其余写入（快照、积分史、审计）的原子性。
- **仅应用层串行化（全局锁/单写者）**：把所有并发压到一个队列，
  吞吐与可用性双输。

## 后果

- 每个新不变量先问"约束能不能表达"；表达不了的才用行锁，锁的获取
  顺序必须写进服务模块头（锁序是并发代码的接口，不是实现细节）。
- 已知代价：偶发死锁窗口存在（任何多锁系统都有），靠统一锁序压缩；
  首次容量战役观测到的 upload-intent 死锁已按"证据门控"立项
  （ADR 0001 的移交记录 + PR #154 的取证设施）——下一次出现将带着
  完整锁获取史落袋，修复走确定性回归测试。
