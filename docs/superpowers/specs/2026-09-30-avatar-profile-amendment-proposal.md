# Avatar 能力提案（缺陷 #4 后端半）— spec 修订草案

> 状态：**待 owner 裁定**（2026-09-30，测试缺陷 #4「用户 avatar 和 nickname 缺失」）
> 已核查现状：nickname 修改 / 手机换绑 / 邮箱验证 / 登录态改密码（`/me/password`）后端全部已存在；**唯 avatar 无任何支持**（模型/接口/存储全缺）。本文只裁 avatar；页面重构归前端。

## 提案要点（按决策点列出）

### D1 存储与模型
- `users.avatar_object_key TEXT NULL`（迁移 0023），对象键 `avatars/{user_id}/{uuid}.{ext}`，存 MinIO 主桶。
- **object key 永不进入任何 DTO/日志/学生端 DOM**（G 隐私纪律，同提交文件）。

### D2 上传（`POST /api/v1/me/avatar`）
- **multipart 直传后端**（非提交通道的 write-once 预签名——头像 ≤2MB 且服务端需校验，直传更简，不引入预签名/意图清理复杂度）。
- 限制：≤ 2MB；类型 png / jpeg / webp，**按魔数校验**（头部字节即可，无需图像库——遵守"不引入新依赖"）。
- 前端裁剪为方形后上传；后端不转码、不生成缩略图（V1）。
- 频控：每账号 10 分钟 1 次（防刷）；审计事件 `USER_AVATAR_CHANGED`（不含图片数据）。
- 覆盖语义：重复上传 = 替换（旧对象由既有清理机制之外的独立删除——V1 直接同步删除旧对象，保留新键）。

### D3 展示（`GET /api/v1/users/{id}/avatar`）
- **后端流式代理** MinIO 字节（登录可见——评论区/排行榜展示头像需要跨用户读取）。
- `ETag`（对象键哈希）+ `Cache-Control: private, max-age=86400`；304 支持。
- **绝不 302 到预签名 URL**（会把 object key 暴露进 DOM/网络层）。

### D4 DTO
- `MePublic` 与公开画像 DTO（评论作者、排行榜条目等）增加 `has_avatar: bool`；前端按约定路径拼 avatar 端点 URL。

### D5 删除（`DELETE /api/v1/me/avatar`）
- 置 NULL + 删对象；前端回退默认头像（首字母生成，纯前端）。

### D6 明确不做（V1 边界）
- 不做内容审核（图片鉴黄等）——与现有昵称同级的信任模型；不做出图变体/CDN；不做管理员强制换头像。

## 测试与验收（实现时）
- 集成：上传（合法/超限/错类型魔数不符）、替换、删除、跨用户读取（登录态）、ETag 304、审计行、key 不出现在任何响应。
- 迁移 0023 gate；`verify-migrations` PASS。
