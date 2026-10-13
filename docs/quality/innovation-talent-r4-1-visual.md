# R4.1 人才视觉变更与基线走查

2026-10-13。用户本轮明确要求人才页第一轮UI优化，短方案已在实施前展示。本次为批准的产品视觉变更，不是等价迁移或字体归一化；只更新人才基线并独立提交，不调整0.01容差、不新增axe豁免。复用原tokens与Noto Sans CJK SC钉定。

旧图完整保留于工作区 `.local-dev/r4-1-visual-before/`；前后对照图 `.local-dev/r4-1-visual-review/`，逐图尺寸/字节比对 `.local-dev/logs/r4-1-visual-differences.json`。基线捕获使用既有独占测试库/Redis9的固定标签世界，真实上传及扫描；该世界自行建/清理测试数据，不清开发库。

原基线比较4项失败（两个新图缺基线、三个旧场景在首个改变截图处失败），完整日志 `r4-1-browser-visual-before.log`；不能把此运行算通过。获准更新运行4 passed，`r4-1-browser-visual-update.log`；此为捕获，不代替最终无update的比较。

13张：8张批准的布局变化、3张逐字节一致、2张新增。代理已视觉查看正常列表、手机详情、本人桌面表单、手机预览/联系、空库及两张长内容的前后对照；byte-identical图使用完整文件字节比较。手机未出现横向溢出，自动axe与原确认焦点验证见人才场景；没有实际屏幕阅读器测试。

| 图（Linux） | 旧→新尺寸 | 判定与内容核对 |
| --- | --- | --- |
| `talent-contacts-mobile-linux.png` | 366×237 → 366×238 | 联系区文字/动作保持；详情分组使元素裁切边界高1px。前后对照可见文本一致，未改联系权限或容差。 |
| `talent-detail-mobile-linux.png` | 390×865 → 390×1167 | 身份/状态/技能分开；新增材料含义；优势、合作、经历分组及左对齐正文。 |
| `talent-empty-mobile-linux.png` | 390×1315 → 390×1282 | 原生材料筛选、横向换行技能项、材料说明；空库语义保持。 |
| `talent-error-mobile-linux.png` | 390×844 → 390×844 | 逐字节一致：无产品/字体/布局变化。 |
| `talent-list-desktop-linux.png` | 1440×1362 → 1440×1000 | 同排关键词/合作/材料筛选、两行技能选择、优势优先；首个结果进入900px首屏。 |
| `talent-loading-mobile-linux.png` | 390×844 → 390×844 | 逐字节一致：无产品/字体/布局变化。 |
| `talent-long-detail-mobile-linux.png` | 390×2645 → 390×2832 | 长姓名/正文保持换行；阅读分组/材料说明增加高度；0小时与未填保持区分。 |
| `talent-long-form-mobile-linux.png` | 366×3524 → 366×3797 | 字段分组、触摸标签、新生提示和文件说明；窄屏身份字段单列，长词不溢出。 |
| `talent-materials-detail-mobile-linux.png` | 新增 → 390×1269 | 新增：真实已公开PNG附件的中性标志、未核实说明及主动下载。 |
| `talent-materials-list-desktop-linux.png` | 新增 → 1440×1030 | 新增：有材料筛选、同源标志/摘要；未加载附件字节或联系值。 |
| `talent-owner-desktop-linux.png` | 992×3196 → 896×2964 | 56rem阅读宽度、三列身份、横向技能、合作意向/经历/作品/联系分组，按钮层级。 |
| `talent-preview-mobile-linux.png` | 358×722 → 358×812 | 分组私有预览/技能/材料含义；长预览可滚动，原确认/取消及焦点行为经E2E保持。 |
| `talent-unavailable-mobile-linux.png` | 390×844 → 390×844 | 逐字节一致：无产品/字体/布局变化。 |

最终五组比较、完整浏览器与当前源码对应证据以 `innovation-talent-r4-1-verification.md` 为准。原任务、成果、导航、招募基线没有重拍；所有图均只包含具名模拟资料。全页截图中固定底导航仍在实际视口底部，长页面以滚动可达和键盘验证为补充，不用单张全页图冒充完整可操作性验收。
