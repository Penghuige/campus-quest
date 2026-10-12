# 创新创业 F01–F07 修复独立评审

2026-10-12。评审按 requesting-code-review，由独立 Astra High 上下文只读执行，未修改工作区、索引或分支；未安排实施代理。

## 首次结论：REQUEST_CHANGES

范围 `c4e2e043bc88a4f678a490ba7ba3c6b773d581f4..33ff21b8158daaa6e6173ac35d7ca59aa63f7e2b`。Critical 0，Important 2，未另报 Minor。

1. P1：原保存 A 延迟→站内返回→显式重试 A 先成功→新输入 B→原 A 回执迟到。原组件仅按项目/认证 epoch 删除恢复记录，会误删 B。恢复清理需要匹配命令拥有的输入快照。
2. P2：其他阶段的超长创意字段纠错时，第一个按键清空错误，textarea 立即消失，无法连续输入。原 E2E 的原子 fill 没覆盖逐字修改。

两项均核实接受。新增真实延迟回执浏览器测试和缓存单测先失败；原隐藏字段测试改为逐字输入先失败。完整 RED 输出在工作区 `.local-dev/logs/qa12-review-delayed-red-v2.log`、`qa12-review-recovery-red.log`、`qa12-review-typing-red-v2.log`。

## 定点复审：APPROVE

最终产品提交 `457f09b9ddbd55685190293344e1f62e304b9e05`。同一独立评审上下文复查 `33ff21b..457f09b`：Critical / Important / Minor 均无新增发现，两项关闭。

- clearRecruitmentRecovery 按认证 epoch 与不可变 form 对象身份 compare-and-delete。save/publish/reopen 回执传入该命令拥有的 form；显式丢弃和认证清理仍可完全清除。
- 独立 ideaCorrections 展示状态使控件保留至成功保存或显式阶段切换，修改错误文案不再使字段卸载。
- 真实 E2E 顺序包含：原 POST 已提交但响应被暂留；相同 request_id 重试成功（200）；新输入 B；释放原回执；再次导航返回保留 B。
- 逐字纠错覆盖 2003 字→Backspace 后仍 2002 字且焦点保持→全选逐字替换→保存后收起。

评审读取了 RED 与 GREEN 证据：9 项定点 E2E、6 项恢复/未知命令单测通过，Windows 670 项单测及八门禁退出 0；评审未独立重跑这些测试。复审通过只评价最终源码，不替代执行者最终 Linux/全站/像素验证。

## 审查边界裁定

- R4、后端全域业务及成果复审：未修改，超出本批七项问题，保持原规则。
- 最终 Linux、全站浏览器、像素：由执行者完成并在本轮验证回执记录，不冒充评审已执行。
- 实际屏幕阅读器：未执行；键盘、DOM 关联、live status 与 axe 的通过不能等同真人读屏通过。
- 合并：未授权，当前 PR 保持 Draft；精确提交的远端 CI 另行查询。
