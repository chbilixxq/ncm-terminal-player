# 网易云音乐 · 终端播放器

一个**纯 Python 标准库**实现的网易云音乐命令行播放器：无 GUI、无第三方依赖、单文件启动，
在终端里完成 **搜索、歌单、排行榜、每日推荐、私人 FM、扫码/密码登录、逐行歌词、播放队列** 等完整功能，
并能根据终端尺寸自动在「宽屏双栏 / 中屏堆叠 / 窄屏极简」之间切换布局。

**许可证：CC BY-NC-SA 4.0**（署名-非商业性使用-相同方式共享）——
任何人都可以**自由地为非商业目的**使用、修改与再分发本项目，详见
[许可证与免责声明](#7-许可证与免责声明) 与 [LICENSE](LICENSE)。

```bash
python3 netease_player.py
```

> 只需要 Python 3.8+。播放出声需要系统里有 mpv / ffplay / mpg123 中的任意一个
> （都没有时会自动降级为“模拟播放”：进度、歌词、队列照常工作，只是不出声）。

---

## 1. 快速开始

```bash
# 1) 直接启动（交互界面）
python3 netease_player.py

# 2) 查看参数
python3 netease_player.py --help

# 3) 离线自检（不需要联网：加密 / 排版 / 歌词 / 布局 / 二维码 / 按键解析）
python3 netease_player.py --selftest

# 4) 脚本 / 管道模式
echo "search 海阔天空" | python3 netease_player.py --plain
python3 netease_player.py -c "search 海阔天空" -c "play 1" --plain
```

### 播放内核

| 内核 | 能力 | 说明 |
| ---- | ---- | ---- |
| `mpv` | 暂停 / 跳转 / 音量 / 精确进度 | **推荐**，通过 IPC 精确控制 |
| `ffplay` | 暂停 / 跳转 / 音量（重启生效） | 没有 ffplay 时自动改用 `ffmpeg`（同一套参数） |
| `mpg123` | 暂停 / 跳转 | 轻量后备 |
| `sim`  | 全部 | 模拟播放：无音频环境、自动化测试用 |

> 自动兜底：① 直连流地址播放失败 → **自动下载到本地缓存**再播（顺带解决地址过期、
> CDN 拒绝 UA、个别 ffmpeg 对网络流崩溃等问题）；② 播放内核连续出错 → 自动降级到下一个内核。
> 缓存目录 `cache/`（配置目录内）自动保留最近 30 首。

```bash
python3 netease_player.py --backend mpv      # 启动时指定内核
backend ffplay                               # 界面内切换
backend auto                                 # 自动探测（默认）
```

启动时会做一次**能力探测**（播一小段静音 WAV），探测失败自动换下一个内核；
`mpv` 还会先试系统音频输出、不行再退回 `--ao=null`（无音频设备的机器/容器也能跑）。

---

## 2. 命令一览

进入界面后输入 `help` 查看（支持 Tab 补全、上下方向键翻历史、PgUp/PgDn 翻输出）：

### 播放控制
| 命令 | 说明 |
| ---- | ---- |
| `search <关键词> [song/artist/album/playlist] [页码]` | 搜索，别名 `s` |
| `more` | 搜索结果翻下一页 |
| `play <序号｜all｜歌曲ID｜关键词>` | 播放；不带参数=继续播放 |
| `pause` / `next` / `prev` / `stop` | 暂停·继续 / 下一首 / 上一首 / 停止 |
| `seek <+秒｜-秒｜mm:ss｜百分比>` | 跳转进度 |
| `vol <0-100｜+N｜-N>` | 调节音量 |
| `mode <list｜one｜seq｜shuffle>` | 列表循环 / 单曲循环 / 顺序 / 随机 |
| `list` | 查看播放队列**全部歌曲**（▶ 为当前曲目；超出窗口自动全屏浏览） |
| `list clear` | 一键清空播放队列并停止播放 |
| `retry` | 重新加载当前歌曲（换源 / 换音质） |

### 发现音乐
| 命令 | 说明 |
| ---- | ---- |
| `top [序号]` | 排行榜列表 / 直接打开某榜曲目 |
| `pl <序号｜歌单ID> [play]` | 打开歌单（`top/rec/mine` 的结果用序号即可） |
| `artist <序号｜ID>`、`album <序号｜ID>` | 歌手热门歌曲 / 专辑曲目 |
| `daily [pl]` | 每日推荐歌曲（默认）或推荐歌单 |
| `fm`（`fm trash`） | 私人 FM（垃圾桶丢弃当前曲） |
| `rec` | 推荐歌单 |
| `mine`、`likes` | 我的歌单 / 我喜欢的音乐（需登录） |

### 歌词与信息
| 命令 | 说明 |
| ---- | ---- |
| `info` | 当前歌曲详情（专辑 / ID / 音质 / 版权 / 进度） |
| `lyric` | 打印当前歌词上下文 |
| `lyric on/off` `lyric trans` `lyric roman` | 翻译开关 / 罗马音开关 |
| `lyric offset +0.5` | 歌词时间轴微调 |
| `comment`（`cm`） | **全屏评论页**：翻页浏览全部评论（热评置顶），`i` 发表评论（需登录） |

### 账号
| 命令 | 说明 |
| ---- | ---- |
| `login qr` | **扫码登录**：进入**全屏二维码视图**（整屏独占、不会被输出窗口截断），`q` 返回后后台仍继续等待，再次 `login qr` 可重新打开；终端放不下或禁用彩色时自动给出登录链接 |
| `login pass <手机号>` | 手机号登录（密码掩码输入，不进历史记录） |
| `login email <邮箱>` | 邮箱登录 |
| `login sms <手机号> [验证码]` | **应用内短信验证码登录**：不带码先发短信，程序内输入验证码完成登录（支持 `+852` 等国际号码） |
| `login cookie <MUSIC_U>` | 导入**浏览器已登录的凭证**（F12 复制 MUSIC_U，可整段粘贴），让程序内直接登录 |
| `me` / `logout` | 账号信息 / 退出登录 |

### 界面与其它
`clear`、`theme color|mono`、`backend …`、`quality standard|higher|exhigh|lossless`、`about`、`quit`

---

## 3. 交互细节

* **布局自适应**：宽度 ≥96 列时左侧“正在播放 + 队列”、右侧“歌词”双栏；
  64–95 列上下堆叠；更窄时退化为两行状态 + 歌词行。高度不足时自动压缩面板。
  终端尺寸用「SIGWINCH + 每帧轮询 ioctl」双保险检测——在不投递 SIGWINCH 的
  终端复用器 / 容器环境里也能正常跟随窗口变化。
* **输出缓冲与翻页**：列表类命令（`list`/`search`/`likes`/`top`/`pl`/`daily`…）
  一律**完整列出所有条目**；输出一旦超过窗口高度，会**自动切入全屏浏览**
  并定位到本次输出的开头，`j/k` 翻行、`PgUp/PgDn/空格` 翻页、`g/G` 首尾、
  `q` 返回。任何时候也可以手动 `PgUp` / `Ctrl-B`（或命令 `view`）进入浏览，
  `Ctrl-L` 重绘。
* **快捷键**：`↑↓` 历史、`Tab` 补全、`Ctrl-A/E` 行首行尾、`Ctrl-U` 清空、
  `Ctrl-W` 删词、`Ctrl-C` 清空输入、`Ctrl-D` 退出、`quit/exit/q` 退出。
* **全角宽度**：中文/日文按 2 列计算，表格、进度条、面板边框在中英混排下对齐。

---

## 4. 配置与数据

配置目录（自动创建，按优先级）：

1. `$XDG_CONFIG_HOME/ncm-player`
2. `~/.config/ncm-player`
3. 脚本目录下 `.ncm-player`
4. `$TMPDIR/ncm-player`

| 文件 | 内容 |
| ---- | ---- |
| `config.json` | 音质 / 音量 / 播放模式 / 主题 / 内核偏好 |
| `cookies.txt` | 登录凭证（Netscape cookie 格式，**请勿外传**） |
| `device.json` | 设备标识（模拟客户端所需） |
| `history.txt` | 命令历史 |

环境变量：`NCM_CONFIG_DIR`（指定配置目录，便于便携部署 / 测试隔离）、
`NO_COLOR`（禁用颜色）、`COLUMNS`/`LINES`（管道模式的布局）、
`NCM_DEBUG`（命令出错时打印堆栈、输出 resize 调试信息）。

---

## 5. 常见问题

**没有声音？**
先看启动提示里的“播放内核”。三选一安装即可：

```bash
sudo apt install mpv        # 推荐
sudo apt install ffmpeg     # 提供 ffplay
sudo apt install mpg123
```

**提示“无法播放 / 暂无版权 / VIP 专享”？**
部分歌曲受版权或会员限制。可用 `quality standard` 降到 128k 再 `retry`；
仍然失败说明该账号/地区确实无权播放本曲。

**进度条只有 30 秒？**
该歌曲对当前账号是 **VIP 试听**（接口只下发 30 秒片段），程序会按真实可听长度显示进度；
登录并开通会员即可播放完整版（`login qr`）。

**提示“直连播放失败，改为下载到本地后播放”？**
正常兜底：先把音频下载到缓存目录再播放，日志里会显示
`使用本地缓存播放：xxxx.mp3`。

**接口返回“操作过于频繁，被风控拦下了”？**
短时间大量请求会被限流，稍等片刻再试；本程序已内置指数退避重试。

**扫码登录打不开？** 首选用网易云音乐 App 扫码（视图底部会实时显示
等待/已扫码/成功状态）。二维码同时给出链接
`https://music.163.com/login?codekey=…`：在**手机**上打开可完成确认；
在**电脑浏览器**打开登录的是网页版（登录态留在浏览器里），若想让程序内也登录，
执行 `login cookie <MUSIC_U>` 把浏览器凭证导入即可。

**评论页怎么用？** 输入 `cm`（或 `comment`）进入全屏评论页：
`j/k` 翻行、`PgUp/PgDn/空格` 翻页、`g/G` 首尾、`l` 加载下一页、`r` 刷新、
`q` 返回；按 `i` 写评论、回车发表（需先 `login qr` / `login sms` /
`login cookie` 登录，未登录会提示），`Esc` 取消输入。热评排在最前。

**短信验证码发不出去/登录被拒？** 常见原因是发送过于频繁或触发风控：
稍等片刻重试即可；若服务端要求图形验证码（终端里不便完成），
提示里会说明，可改用 `login qr` 或 `login cookie`。国际号码用
`login sms +852xxxxxxxx` 形式。

**关掉二维码视图后还能再打开吗？** 可以：输入 `login qr` 会生成新的二维码并
取代旧的等待（旧轮询自动结束）；也可以直接 `q` 返回，后台继续等扫码。

**终端太窄 / 行数不够显示不了二维码？** 二维码需要约 45 列宽 × 22 行高，
放不下时程序会给出明确提示和可直接打开的登录链接；放大终端后重新 `login qr` 即可。
注意二维码靠黑白底色区分深浅：`NO_COLOR=1`、`--mono` 或主题 `mono` 下无法渲染，
会自动改用链接登录（提示里会说明原因）。

**扫码视图能退出吗？** 按 `q`/`Esc` 即可返回提示行，后台仍会继续等待扫码，
结果照常打印；扫码成功会自动退出视图并保存凭证。

---

## 6. 项目结构与测试

```
netease_player.py        # 全部实现（单文件，纯标准库）
LICENSE                  # CC BY-NC-SA 4.0 完整法律文本 + 版权声明
LICENSES/
  python-qrcode-BSD-3-Clause.txt   # 内嵌第三方组件的许可证副本
README.md                # 本文档
tools/
  backend_test.py        # 真实播放后端测试（mpv / ffplay，各 11 项）
  tui_sim_test.py        # 进程内 TUI 仿真测试（布局/命令/翻页/登录，44 项断言）
  tui_test.py            # 伪终端(PTY)驱动的端到端测试（26 项，需 /dev/ptmx）
  ffbin/                 # 可选：静态 ffmpeg（用于本地验证 ffplay 后端）
  asoundrc               # 可选：无声环境的 ALSA null 设备配置
screenshots/             # 测试产生的屏幕快照
```

```bash
python3 netease_player.py --selftest   # 离线自检（10 项）
python3 tools/tui_sim_test.py          # TUI 仿真：布局/命令/翻页/登录（44 项）
python3 tools/backend_test.py          # 真实后端：推进/暂停/跳转/自动切歌（11 项）
python3 tools/tui_test.py              # PTY 端到端：真实终端/翻页/resize（26 项）
python3 tools/make_shots.py            # 生成彩色终端截图（screenshots/*.html）
python3 tools/resize_debug.py          # 诊断：resize 时子进程的输出
python3 tools/pty_size_test.py         # 诊断：PTY 尺寸传播与信号送达
```

截图预览：`screenshots/preview-wide.png`（140×40 宽屏双栏）、
`screenshots/preview-narrow.png`（64×22 中屏堆叠）、
`screenshots/preview-browse.png`（全屏翻页回看长输出）、
`screenshots/preview-qr.png`（`login qr` 全屏二维码视图）。

实现要点：

* **weapi 加密**：内置纯标准库 AES-128-CBC（S 盒由有限域构造，含 NIST 测试向量自检）
  与 RSA 模幂，登录、每日推荐、私人 FM 等加密接口可直接调用。
* **播放引擎**：队列 / 四种播放模式 / 地址缓存与音质回退 / 预取下一首 /
  连续失败自动切换内核；后端通过统一抽象接入，`mpv` 走 IPC，`ffplay` 走
  SIGSTOP 暂停 + `-ss` 重启跳转 + 墙钟进度。
* **歌词**：LRC 解析（多时间戳、offset 标签）、翻译/罗马音合并、按播放位置滚动高亮。
* **二维码**：扫码登录的 QR 矩阵由内嵌的 `python-qrcode`（BSD-3-Clause，
  Copyright (c) 2011, Lincoln Loop）生成，用半块字符 `▀` + 黑白底色两行合一渲染。

---

## 7. 许可证与免责声明

### 许可证

本项目的源代码、文档与截图均以 **CC BY-NC-SA 4.0**
（Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International，
署名-非商业性使用-相同方式共享 4.0 国际）发布，完整法律文本见 [LICENSE](LICENSE)。

| | 说明 |
| ---- | ---- |
| ✅ 可以 | 个人学习、研究等**非商业**目的自由使用、复制、修改、再分发 |
| ✅ 可以 | 二次开发后以相同协议（CC BY-NC-SA 4.0）发布你的衍生版本 |
| ❌ 不可以 | 任何商业用途（售卖、付费课程/服务、捆绑商业产品等） |
| ❌ 不可以 | 修改后以与原协议不兼容的条款（尤其专有/商业条款）发布 |
| ⚠️ 需要 | 再分发时**署名**：保留本仓库 `LICENSE` 与版权信息，并注明原始作者 |

署名建议写法（可按需调整）：

> 基于 ncm-terminal-player 的网易云音乐终端播放器二次开发，
> 原作者：\<你的名字\>，采用 CC BY-NC-SA 4.0 许可协议发布。

「相同方式共享」意味着：你修改/扩展后的版本必须同样以 CC BY-NC-SA 4.0
（或兼容协议）发布，不能闭源或改为商业条款——这保证了任何人（包括你）
的改动都始终留在"自由、非商业"的生态里。

> 说明：CC 系列协议不是 OSI 认证的开源协议，因为它限制了商业使用；
> 如果你希望允许商业使用，可以改用 MIT / Apache-2.0 等协议（本项目
> 其余部分无专利限制，可自行选择）。

### 第三方组件

| 组件 | 许可证 | 说明 |
| ---- | ---- | ---- |
| [python-qrcode](https://github.com/lincolnloop/python-qrcode) | BSD-3-Clause | 仅使用其纯标准库部分生成扫码登录二维码，源码内嵌于 `netease_player.py`，版权声明保留在 `_QRCODE_LICENSE` 中，副本见 `LICENSES/python-qrcode-BSD-3-Clause.txt` |

其余功能全部由 Python 标准库实现，无其它第三方依赖。

### 免责声明

* 本许可证只覆盖**本项目的代码与文档**，不涉及网易云音乐的内容、商标或接口授权；
  音乐内容的版权归原版权方所有。
* 本项目通过网易云音乐的公开 Web 接口获取数据，仅供学习与个人使用；
  请遵守网易云音乐服务条款，不要用于批量抓取或商业用途。
* 本项目按"现状"提供，不附带任何明示或暗示的担保；因使用本项目产生的任何
  后果（含账号风控、封禁、数据损失等）由使用者自行承担。
* 登录凭证（`cookies.txt` 等）属于你的账号机密，请勿外传或随代码分发。

---

本项目由Xiaomi Mimo 2.6 Flash 使用 Deepseek Harness构建
