# CampusQuest Refactoring Practices

适用于减少重复、改善模块边界和清理注释的行为保持重构。
新功能、缺陷修复和产品语义变更走各自 spec/plan，不套用本清单扩大范围。

## 执行清单

### 1. 先固定可观察契约

- 要求：修改前列出公开 API、错误码、状态机、事务、权限、隐私、UI 选择器和文案等不变量，并记录当前测试基线。
- 理由：重构改变内部结构，不改变外部行为；没有契约就无法证明等价。
- 来源：[Martin Fowler, Refactoring](https://refactoring.com/)；`docs/quality/quality-gates.md` §1、§6-§14。
- 日期：2026-10-27。

### 2. 只消除真实的业务知识重复

- 要求：优先合并同一状态机、校验或副作用序列的多份实现；
  保留因角色、契约或风险不同而存在的显式分支。
  优先类内或私有 helper，不为相似语法引入通用框架。
- 理由：DRY 的收益是单点维护业务规则；过度抽象会把调用方必须知道的不变量藏进浅模块。
- 来源：`docs/quality/backend-engineering.md` §1-§5；`docs/quality/frontend-patterns.md` §16。
- 日期：2026-10-27。

### 3. 保持 diff 聚焦

- 要求：每批限定一个可独立验收的目标；不全库格式化，不顺手调整无关代码，不按文件长度或复杂度指标直接拆分。
- 理由：小步修改更容易审查、定位和回退；复杂代码可能对应必要的业务约束。
- 来源：[Django coding style](https://docs.djangoproject.com/en/dev/internals/contributing/writing-code/coding-style/)
  关于避免无关格式化重构；`AGENTS.md` Development discipline。
- 日期：2026-10-27。

### 4. 用行为测试保护重构

- 要求：先运行现有行为测试；新增行为先写失败测试。每步后跑聚焦测试，完成前跑所属模块门禁。
  并发、幂等等高风险不变量仍需真实 PostgreSQL。
- 理由：行为保持只能由可观察结果证明，不能由阅读 diff 或函数身份推断。
- 来源：`docs/quality/quality-gates.md` §1、§7-§14；`.claude/skills/campusquest-test-process/SKILL.md`。
- 日期：2026-10-27。

### 5. 保护生成物、历史证据和本地环境

- 要求：生成物只由对应命令更新；历史计划、审查证据和仍有效的限定词不按文风删除；
  不用项目代码掩盖本地配置问题，也不擅自修改 Git/系统配置。
- 理由：生成物需要单一来源，历史记录和本地环境不属于重构对象。
- 来源：`AGENTS.md` Generated code、Test artifacts manifest；`docs/quality/agent-tooling.md` §13。
- 日期：2026-10-27。

### 6. 以新鲜证据和复审收口

- 要求：交付列出实际修改、运行命令和结果、未运行检查及原因；完成有意义实现后请求代码复审。无法执行的检查不得写成通过。
- 理由：当前 head 的独立验证和审查才是可检查证据，旧日志不能替代。
- 来源：`docs/quality/quality-gates.md` §14-§15；`docs/quality/agent-tooling.md` §13。
- 日期：2026-10-27。

执行顺序：契约与基线 → 小步修改 → 聚焦测试 → 模块门禁 → 复审。
任何一步暴露新的产品语义或跨模块取舍时，暂停并重新确认范围。
