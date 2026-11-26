"""玩家头图、默认模型、来源和缓存的定向变异。"""
import sys

from mutation_all import Case


def mutation_cases():
    python = (sys.executable, "-B", "-X", "utf8")
    backend = (*python, "tests/test_v0420_player_avatars.py")
    browser = (*python, "tests/ui_player_admin_check.py", "AV6")
    skins = "core/skins.py"
    html = "pages/mc_control/index.html"
    return [
        Case("AV1", "把默认模型的 UUID 低位固定成 0", skins,
             'return "alex" if hash_code & 1 else "steve"',
             'return "alex" if hash_code & 0 else "steve"',
             (*backend, "AvatarChecks.test_java_uuid_and_model"), "AV1 默认模型逐条匹配 Java 真值", backend),
        Case("AV2", "UUID 退回随机生成", skins,
             '    return UUID(bytes=hashlib.md5(payload, usedforsecurity=False).digest(), version=3)',
             '    from uuid import uuid4\n    return uuid4()',
             (*backend, "AvatarChecks.test_deterministic"), "AV2 同名头像属性必须稳定", backend),
        Case("AV3", "服务端头像模块引入网络依赖", skins,
             "import hashlib", "import hashlib\nimport urllib.request",
             (*backend, "AvatarChecks.test_no_network"), "AV3 服务端头像算法禁止使用网络", backend),
        Case("AV4", "管理名单丢掉头像属性", "core/player_admin.py",
             '    result["avatars"] = online_avatars(players)', '    result["avatars"] = {}',
             (*backend, "AvatarChecks.test_online_payloads"), "AV4 管理名单须带头像属性", backend),
        Case("AV5", "只读名单丢掉头像属性", "core/web_api.py",
             '                    info["list"]["avatars"] = player_admin.online_avatars(players)',
             '                    info["list"]["avatars"] = {}',
             (*backend, "AvatarChecks.test_online_payloads"), "AV5 只读名单须带头像属性", backend),
        Case("AV6", "成功也不替换默认头图", html,
             'image.onload = () => settle("hit");', '// omit load',
             browser, "AV6 头图成功后须替换默认头像", browser),
        Case("AV7", "删除头图失败处理", html,
             'image.onerror = () => settle("miss");', '// omit error',
             browser, "AV7 缺图须保留正确默认头像并记录 miss", browser),
        Case("AV8", "首帧头像一律使用 Steve", html,
             'image.src = model === "alex" ? DEFAULT_AVATAR.alex : DEFAULT_AVATAR.steve;',
             'image.src = DEFAULT_AVATAR.steve;',
             browser, "AV8 先按后端模型显示内置头像", browser),
        Case("AV9", "重复绘制时丢掉节点复用缓存", html,
             '  if(existing){\n    if(existing.state === "hit") displayRemoteAvatar(slot, existing.image);\n'
             '    return;\n  }',
             '  if(existing && existing.state !== "hit") return;',
             browser, "AV9 监控轮询和重绘须复用头图，不得重复请求", browser),
        Case("AV10", "忽略持久缓存的 miss", html,
             'if(cached?.state === "miss"){', 'if(false){',
             browser, "AV10 有效的缓存 miss 跨文档不得再请求", browser),
        Case("AV11", "缓存永久有效", html,
             'if(saved?.expiresAt > now && (saved.state === "hit" || saved.state === "miss")) return saved;',
             'if(saved && (saved.state === "hit" || saved.state === "miss")) return saved;',
             browser, "AV11 缓存过期后须重新尝试", browser),
        Case("AV12", "只读界面忽略后端头像属性", html,
             '    avatars: legacy.avatars,', '    avatars: {},',
             browser, "AV12 只读列表须使用后端模型", browser),
        Case("AV13", "在线列表未转义名字", html,
             '${AVATAR_SLOT}${escServer(name)}</span>', '${AVATAR_SLOT}${name}</span>',
             browser, "AV13 玩家名须按文本显示", browser),
        Case("AV14", "非法名字也渲染为普通头像", html,
             'if(typeof name !== "string" || !PLAYER_NAME.test(name) || name.trim() !== name){', 'if(false){',
             browser, "AV14 非法或空名字须显示字母瓦片", browser),
        Case("AV15", "头像图片改回旧源", html,
             '"https://minotar.net/helm/" + encodeURIComponent(key) + "/32.png"',
             '"https://mc-heads.net/avatar/" + encodeURIComponent(key) + "/32"',
             browser, "AV15 有皮肤的正版名须经 Minotar 返回真皮肤", browser),
        Case("AV16", "删除 Minotar 占位图比对", html,
             "if(isMinotarPlaceholder(bytes)) return null;", "// treat the placeholder as a skin",
             browser, "AV16 Minotar 的 200 占位图须保留内置默认头像", browser),
        Case("AV17", "沿用旧源的缓存键", html,
             'const AVATAR_CACHE_PREFIX = "mcavatar2:";', 'const AVATAR_CACHE_PREFIX = "mcavatar:";',
             browser, "AV17 旧源的 miss 不得拦截 Minotar 查询", browser),
        Case("AV18", "缺图用六小时缓存", html,
             "const AVATAR_MISS_TTL = 30 * 60 * 1000;", "const AVATAR_MISS_TTL = AVATAR_HIT_TTL;",
             browser, "AV18 命中保留六小时，缺图半小时后复查", browser),
        Case("AV19", "只按长度识别 Minotar 占位图", html,
             "  return bytes.length === MINOTAR_PLACEHOLDER.length &&\n"
             "    bytes.every((byte, index) => byte === MINOTAR_PLACEHOLDER[index]);",
             "  return bytes.length === MINOTAR_PLACEHOLDER.length;",
             browser, "AV19 同长异图不得判为占位图", browser),
    ]
