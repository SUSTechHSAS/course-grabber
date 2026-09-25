# 教务系统抢课脚本

对接常见的「志愿制」选课系统：**放课瞬间精准首发 + 持续捡漏**，带自动登录、
被踢重登录、写请求限流节拍器，以及对服务器过载的容错。

> 这是一个**配置驱动**的工具：域名、接口路径、Cookie 名、密码加密密钥、候选教学班
> 全部写在本地 `config.json` 里（已被 `.gitignore` 忽略），仓库代码里没有任何具体学校的信息。

```
                    ┌──────────────┐
   学号 + 密码 ───► │  自动登录     │ ──► 会话(token + cookie)
                    │  school_auth │        │
                    └──────────────┘        │ 被限流踢掉时自动重登
                            ▲               ▼
   19:50 启动 ──► 只读预检 ──► 对齐服务器时钟 ──► T0 错开首发 3 个班
                                                    │
                                            重试循环（捡漏，只读探空位）
                                                    │
                                       已选课程列表出现目标教学班 = 成功
```

## 它解决什么问题

| 问题 | 做法 |
| --- | --- |
| 放课是**一个瞬间**，晚了就没了 | 用 HTTP `Date` 头做时钟区间估计（不用服务器返回的不可靠时间戳），自旋到点出手 |
| 选课系统是**志愿制**，提交后异步处理 | 轮询处理状态，并且**只认「已选课程列表」**这一个真值 |
| 写接口有**限流**，超了会作废整个会话 | `WritePacer` 节拍器：首发与重试循环共用同一份额度，任何路径都凑不出超额的第 N+1 发 |
| 服务器过载时请求**挂住不回** | 独立短连接 + 短超时 + 「上一发响应回来再发下一发」，一发挂住不拖累其它候选 |
| 会话随时可能被踢 | 自动重登录，只读请求自愈重放；**写请求绝不自动重放** |
| 同一账号只能有一个会话 | 单实例 PID 锁，防止两个进程互相踢 |

## 不想配环境？下载预编译版

到 [Releases](https://github.com/SUSTechHSAS/course-grabber/releases) 下载对应平台的一键包，
解压后直接跑：

```bash
./run.sh          # Linux / macOS
run.bat           # Windows（双击）
```

第一次运行会自动生成 `config.json` 并告诉你该填什么。**不需要装 Python、不需要装
onnxruntime、不需要单独下模型** —— 验证码识别模型和运行时都已经打进那一个可执行文件里了。

| 平台 | 包名 |
| --- | --- |
| Linux x86-64 | `course-grabber-linux-x64.tar.gz` |
| Windows x86-64 | `course-grabber-windows-x64.zip` |
| macOS Apple Silicon | `course-grabber-macos-arm64.tar.gz` |
| macOS Intel | `course-grabber-macos-x64.tar.gz` |

> 可执行文件没有代码签名：macOS 首次运行可能要在「系统设置 → 隐私与安全性」里放行，
> Windows 可能弹 SmartScreen，选「仍要运行」即可。不放心就用源码跑（下面那种方式）。

## 快速开始


```bash
# 1) 配置（一次性）
cp config.example.json config.json
$EDITOR config.json          # 填域名、接口路径、Cookie 名、候选教学班……

# 2) 离线自检（不联网、不需要账号、不需要模型）
python3 tests/test_offline.py

# 3) 验证码识别模型（可选但推荐）
git clone <本项目的姊妹仓库> ../click-captcha-matcher

# 4) 凭据（要用自动登录的话）
mkdir -p ~/.config/course-grabber
cat > ~/.config/course-grabber/credentials.json <<'EOF'
{"student_id": "你的学号", "password": "你的密码"}
EOF
chmod 600 ~/.config/course-grabber/credentials.json

# 5) 演练（只读预检，绝不提交）
python3 grab.py

# 6) 实战：19:50 启动，脚本自己等到 20:00:00.000 首发
python3 grab.py --live
```

见到「预检全部通过」再等放课。**不加 `--live` 永远不会提交任何东西。**

## 常用开关

| 参数 | 说明 |
| --- | --- |
| `--live` | 真正提交。不加就只做只读预检 |
| `--at 20:00:00` | 出手时刻（配置时区）。已过则立刻开打，`--tomorrow` 才等到明天 |
| `--now` | 不等了，立刻开打 |
| `--window 90` | 出手后持续尝试多少秒 |
| `--priority 001,002` | 覆盖配置里的候选顺序（可只写 ID 后缀） |
| `--single` | 首发只打第一优先级那一个班（默认打组内前 3 个） |
| `--stagger 0.10` | 多班首发之间错开多少秒 |
| `--write-timeout 4` | 单发写请求最长占用几秒（爆发期自动压到 2 秒） |
| `--no-relogin` / `--no-chrome` | 关掉自动登录 / 完全不读浏览器 Cookie |
| `--relogin-max 4` | 一次运行最多自动登录几次 |
| `--force` | 忽略单实例锁 |
| `--offline` | 不联网，只打印将要发送的报文 |

**随时可以停**：`Ctrl-C` 是优雅停止（不再发新请求 → 收尾 → 打印结果），再按一次立刻退出。

## 工程笔记（都是实测出来的，不是猜的）

### 写接口的限流模型

在一套实际系统上量到的（换成你自己的学校务必重测，用 `tools/probe_rate_limit.py`）：

- **滚动 1 秒内最多 3 发写请求**，第 4 发会返回「请求过快」并且**当场作废整个会话**。
- 1 发/秒、2 发/秒可以一直打，没有累计上限。
- **只读请求不占写额度**（45 发只读后紧接着写，照常成功）。
- **多发「同时」发 ≠ 多次机会**：同一瞬间打出去的 3 发里只有 1 发会被真正评估，
  另外 2 发拿到的是空 msg 的并发拒绝；**错开 0.06 秒以上**才是 3 次真机会。
- 被限流作废的会话不会自己恢复，但**重登录能立刻刷新额度**。

`WritePacer` 就是把这些变成代码里的硬约束：窗口内最多 N 发、相邻两发至少错开
`min_gap`、并且「遗忘」一条记录要等满 `窗口 + 安全边界`（贴着边界发会踩线）。

### 服务器过载时会发生什么

放课瞬间全校同时提交，服务端会过载：请求挂住不回、网关 5xx、返回 HTML 错误页。
脚本原来在这里会「死等」（一次写请求 15 秒超时 × 内部重试 2 次 = 30 秒），
一个挂住的请求就能吃掉整个窗口。`tools/probe_overload.py` 用一个**会按剧本过载的假学校**
驱动真实的重试循环，量「服务器恢复后多久能确认选中」：

| 过载剧本（20 秒窗口，前 3 秒过载） | 改之前 | 改之后 |
| --- | ---: | ---: |
| 连接挂住不回 | 2 发 / **16.1 秒** | 6 发 / **3.9 秒** |
| 响应慢 6 秒 | 2 发 / **16.1 秒** | 6 发 / **3.9 秒** |
| 网关 502 + HTML | 7 发 / 3.1 秒 | 9 发 / 3.6 秒 |

### 同一账号只有一个会话

连续登录 A→B→C，每次都回头复查前面的会话：

```
登录 A 之后: A=活
登录 B 之后: A=死  B=活
登录 C 之后: A=死  B=死  C=活
```

推论：**跑脚本时别在浏览器里刷新同一套系统**（会把脚本顶下线，反之亦然），
**只能跑一个实例**（脚本用 PID 锁拦住了第二个），也**别指望多开会话把额度翻倍**。

## 安全设计（写在代码里的硬约束）

1. **没有任何退选/删除志愿的代码路径**，全文不含 `operationType":"2"`。
   没有任何函数能移除你已选的课程。
2. **默认不提交**，必须显式 `--live`。
3. **已在已选列表 → 直接退出**，不做任何提交。
4. **白名单**：只提交预检阶段解析出的候选教学班。
5. **只认已选列表**：提交接口说「成功」不算成功，必须已选课程里出现完整教学班 ID。
6. **密码错立刻熔断**：服务端回「登录名或密码不正确」就永久放弃自动登录，
   绝不用错误密码反复试探（避免账号被锁）。
7. **写请求绝不自动重放**：只读请求被判过期会自动重登并重放，写请求不会 ——
   避免重复提交。

## 自检与实验工具

```bash
python3 tests/test_offline.py            # 离线：加密向量、凭据、会话、假学校跑通登录/重登录（35 项）
python3 tools/probe_auth.py --n 20       # 在线真值：用不存在的学号验证「验证码+登录协议」通不通（零账号风险）
python3 tools/probe_rate_limit.py        # 写接口：固定间隔能打几发（只用不存在的教学班 ID）
python3 tools/probe_burst_shape.py       # 连发几发会被限流
python3 tools/probe_concurrent.py        # 同时发 vs 错开发
python3 tools/probe_sessions.py          # 会话共存性 / 重登录能否刷新额度
python3 tools/probe_session_budget.py    # 只读请求是否占写额度
python3 tools/probe_overload.py --all    # 服务器过载时的客户端行为（本地假学校）
GRAB_DEBUG_WRITES=1 python3 grab.py --live --now --window 5   # 逐发打印写请求
```

## 依赖

- Python 3.11+（只用标准库；`--live` 之外的预检也是）
- 可选：验证码识别需要 `onnxruntime` + 姊妹仓库 `click-captcha-matcher`。
  本机解释器没装时会自动寻找可用的解释器（`CAPTCHA_PYTHON` 可指定）。
- **不需要**浏览器：有凭据文件就自己登录。没有凭据时才回退去读本地浏览器的 Cookie。

## 自己打包 / CI

```bash
uv venv .build-venv
uv pip install --python .build-venv/bin/python pyinstaller onnxruntime pillow numpy
CAPTCHA_MODEL_REPO=../click-captcha-matcher .build-venv/bin/python packaging/build.py
# 产物在 dist/ ；加 --no-bundle-model 可以打一个不含识别模型的轻量版
```

CI（GitHub Actions）：

- **push / PR** → 三个平台 × 两个 Python 版本跑离线自检，另加一个"打包冒烟"任务
  （真实构建一次并运行产物，确保打包脚本不会悄悄坏掉）。
- **打 tag（`v1.0.0`）** → 四个平台各构建一个单文件包，自动建 Release 并附
  `SHA256SUMS.txt`。也可以手动 `workflow_dispatch` 触发。

## 许可

MIT。`desencode.py` / `cus_base64.py` 是第三方 MIT 实现（见 LICENSE 的
Third-party notices），算法部分未做改动。

## 免责声明

仅供学习与个人自动化使用。使用前请确认符合你所在学校的网络与选课系统使用规定；
因使用本工具产生的一切后果由使用者自行承担。请勿用于任何破坏系统正常运行的行为 ——
本项目里所有限流相关的研究，目的都是**不要**把服务器打爆、也**不要**把自己的账号打死。
