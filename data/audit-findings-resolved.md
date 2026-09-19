# 审计发现汇总（修复中对照）

## 严重
1. 公告系统前后端断裂：无 is_active 列（toggle→422）、缺 GET 单条（编辑→404）、active 端点要 admin、编辑 is_active 被静默丢弃、announcement.py active property NameError(timezone 未 import)
2. admin.py:142 + users.py:114 删除唯一管理员保护把 property UserModel.is_admin 用于 SQL where → ArgumentError 500
3. 删用户：email_verifications FK 无 cascade → FK violation 500
4. ApiKey.usage_records delete-orphan：删 key 级联删历史用量（DB 设计是 SET NULL 保留）
5. panel.js 公告弹窗非 HTML 模式未 esc → 存储型 HTML 注入
6. 指纹绑定可绕过：已绑定用户缺失 X-Fingerprint 头即放行；key 面指纹逻辑无效（SDK 从不带头）
7. 流式请求上游连接失败 → _stream 未捕获 httpx.HTTPError → 500 且预扣不退
8. 并发计数读-改-写竞态 + 流式响应 return 后立即释放计数（流期间限制失效）
9. 媒体端点（audio/video 二进制响应）JSON 解析失败 → usage=0 → 免费；设计 flat fee 未实现；预扣<flat 时负余额路径
10. Gemini ?alt=sse 不判流式 → 免费；extract_usage 遇 list body AttributeError → 500 且预扣不退
11. admin 设置不生效：register 用 env registration_bonus、buy_package 用 env token_package_rate，从不读 system_settings；max_coin_per_user 纯死代码
12. users.js 余额 u.gavincoin_balance.toFixed TypeError（字符串）→ 用户管理标签页必崩
13. UserOut 缺 last_login_date（users.js 永远显示 –）与 is_admin（删除按钮禁用失效）
14. models.js setTimeout 引用未定义 d → ReferenceError
15. users.py create/update 邮箱不 lower().strip() → admin 创建的用户可能无法登录
16. role pattern 允许 moderator，但 0001 CHECK 约束 role IN (user,admin) → 设 moderator 500
17. API key expires_at/quota 创建后从不强制 → 过期 key 仍可用
18. X-Routed-Via 实际路由模型未用于计费定价（设计 D6，少收风险）

## 中低
19. coin.get_settings() 无 db 时 with get_db()（生成器非 CM）→ TypeError
20. update_settings 循环中逐个 commit + 先写后校验 → 部分写入
21. CLI usage --to < 23:59:59 off-by-one
22. register SMTP 只捕 OSError（SMTPException 漏）→ 注册后 500（用户已建）
23. verify_email 对已删用户 → AttributeError 500
24. login 指纹绑定在非 daily-bonus 路径不 commit → 绑定丢失（边缘）
25. User 模型残留 registration_ip 死注释；fingerprint_hash Mapped[str] 可空不一致；email_verified 模型默认 True vs 迁移 '0' 漂移
26. models/__init__ __all__ 漏 EmailVerification
27. catalog.ready() 条件冗余 + ROUTER_IDS 死常量；平台白名单（D5）未实现=已知设计差距
28. panel.js ctx.config 兜底 tokenRate:10 与默认 100 不符
29. admin.py /admin/users 与 users.py /users 重复管理面（前端只用 /users）
30. README 仍写 GAPI_REGISTRATION_ONE_PER_IP（功能已被迁移 42efb 移除）
31. announcements.js 编辑时间 slice(0,16) 丢时区 → 保存漂移
32. main.py 无默认 jwt_secret / 空 FREELLM_API_KEY 启动警告
33. credit_transactions.reference_id 模型 index=True 但迁移未建索引

## 处置结果（2026-09-17）：全部 33 项已修复
验证：69/69 测试通过（新增 tests/test_phase6_audit.py 20 项回归测试）；
迁移链在全新库从头跑通；uvicorn 冒烟 /health /config /panel 正常。

## 记录在案的已知设计差距（未修，见 CLAUDE.md）
- GAPI_VISIBLE_PLATFORMS 平台白名单（D5）未实现，可见性信任上游目录
- ApiKey.quota 列已存储但无消耗/强制语义（语义未定义）
- settle 允许超过 reserve 的追加扣款（余额可为负，test_phase3 固定的设计行为）
- 代理请求体无大小上限（上游有 25MB 限制，gapi 侧先读入内存）
