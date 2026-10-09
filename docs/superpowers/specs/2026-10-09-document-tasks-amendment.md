# 文档型任务 spec 修订案（QA #12 批次 · owner 批准 2026-10-09）

> 状态：**owner 四裁定之一**（#12-B + docx/pdf + #19 + #22-隐藏可选）；修订案先行，owner 可在 review 时否决设计。
> 改动落点：design spec §10（+新增 §10.1）与 §6.2 字段表。本文是决策记录与实现导引——实现细节以 spec §10.1 为准。

## 裁定内容

1. **`FileType` 宇宙扩展**：`{CSV, XLSX, SQLITE, DOCX, PDF}`
2. **两族互斥**：单任务 `allowed_file_types` 只能属一族——结构化 {CSV, XLSX, SQLITE}（schema 校验族）或文档 {DOCX, PDF}（完整性族）。混合 422。文档型任务 `submission_schema` 必须为空
3. **文档型机器校验 = 完整性检查**，不跑 schema：
   - DOCX：ZIP 本地头 + OOXML manifest，且与 XLSX 可区分（manifest 含 `word/` 前缀成员）；zip 目录可打开
   - PDF：`%PDF-` 头 + `%%EOF` 尾；trailer 可定位
   - 大小上限照常
   - 报告 §12.4 同构（row_count=null，通过则 errors 空；失败一项 FILE_CORRUPT 类）
   - VALIDATED → UNDER_REVIEW 流程不变，内容判断由教师审核承载
4. **前端 picker 按任务收窄** accept（MyClaimResponse/TaskDetail 已带 allowed_file_types）

## 不做的事（边界）

- 不做文档内容解析/文本抽取/关键词检查——V1 明确不做
- 不改变 §14 审核事务与奖励锁流程（文档型与结构化同流）
- 不做模板渲染或文档生成

## 实现派生（后续 PR 的验收锚）

| 项 | 锚 |
|---|---|
| 魔数嗅探 | detect.py 增 DOCX/PDF 判定；XLSX/DOCX 以 manifest 成员区分 |
| 文档校验器 | 完整性（魔数/可解析/大小）；报告单项 |
| 互斥约束 | 任务创建/更新 422；`submission_schema` 空校验 |
| 演示种子 | "复习资料整理"转文档型（DOCX+PDF）；demo 与 e2e world 同步 |
| 矩阵 | submissions 校验矩阵补 D-rows（预期先行纪律） |

## 备选与否决记录

- 「docx/pdf 与结构化混开（单任务多模态）」——否决：picker/报告/审核 UX 双形态复杂度不成比例，教师审阅心理模型割裂
- 「文档型也跑宽松 schema（如文件名正则）」——否决：伪结构化，教师仍需全文审阅，机器层不产生信息增量
