# 部署到 Streamlit Community Cloud（图文步骤）

把这个项目部署到公网（免费、永久），任何人通过链接打开就能用。整个过程不到 5 分钟。

---

## 准备清单

部署前确认：

- ✅ 你有 GitHub 账号（用来登录 Streamlit Cloud）
- ✅ 仓库 `peterli0913/auto_summary_git` 是 **Public**（私有也行，但 Streamlit Cloud 免费版每月只能部署 1 个私有 app）
- ✅ 仓库里有这些文件（已经全部 commit 在 `cursor/hazard-classifier-pipeline-3238` 分支）：
  - `app.py`
  - `requirements.txt`
  - `runtime.txt`（指定 Python 版本）
  - `跑冒滴漏与静电风险专项跟踪.xlsx`（首次启动自动训练用）
  - `hazard_pipeline/`、`scripts/`、`监控巡查情况.xlsx` 等

---

## 步骤 1：登录 Streamlit Community Cloud

打开：**https://share.streamlit.io**

```
┌─────────────────────────────────────────────┐
│ Streamlit Community Cloud                    │
│                                              │
│   [Continue with GitHub]   ← 点这个          │
│   [Continue with Google]                     │
│   [Continue with email]                      │
└─────────────────────────────────────────────┘
```

第一次登录会让你授权 Streamlit 访问 GitHub。一路同意即可。

---

## 步骤 2：新建 App

登录后右上角点 **「New app」** 或 **「Create app」** 按钮。

```
┌─────────────────────────────────────────────────┐
│ Workspace: yourname        [+ New app ▼]  ←     │
├─────────────────────────────────────────────────┤
│  No apps yet. Create your first one.            │
└─────────────────────────────────────────────────┘
```

弹出的对话框会问你两件事：

```
┌─────────────────────────────────────────────────┐
│ Create app                                       │
│   ○ Deploy a public app from GitHub      ← 选这个│
│   ○ Deploy from template                         │
│                                                  │
│   [Continue]                                     │
└─────────────────────────────────────────────────┘
```

---

## 步骤 3：填配置

填入下列三个字段：

```
┌─────────────────────────────────────────────────────┐
│ Repository*                                         │
│ ┌─────────────────────────────────────────────────┐ │
│ │ peterli0913/auto_summary_git                    │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ Branch*                                             │
│ ┌─────────────────────────────────────────────────┐ │
│ │ cursor/hazard-classifier-pipeline-3238          │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ Main file path*                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ app.py                                          │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│ App URL (optional)                                  │
│ ┌─────────────────────────────────────────────────┐ │
│ │ hazard-classifier              .streamlit.app   │ │
│ └─────────────────────────────────────────────────┘ │
│                                                     │
│        [Cancel]            [Deploy! 🚀]   ← 点这个   │
└─────────────────────────────────────────────────────┘
```

| 字段 | 填什么 |
|---|---|
| **Repository** | `peterli0913/auto_summary_git` |
| **Branch** | `cursor/hazard-classifier-pipeline-3238` |
| **Main file path** | `app.py` |
| **App URL** | 自定义子域名，例如 `hazard-classifier`（最终是 `https://hazard-classifier.streamlit.app`），留空会自动生成 |

完成后点 **`Deploy! 🚀`**。

---

## 步骤 4：等待首次部署（约 3-5 分钟）

页面会显示日志，类似：

```
Provisioning machine ... 
Cloning repository ...
Installing dependencies ...
  Successfully installed pandas-... scikit-learn-... jieba-... ...
Starting up ...
   首次启动: 自动训练标准模型 (~30 秒)...
   ✅ 模型已保存到 models/current
You can now view your Streamlit app in your browser.
```

最重要的两行：

- `Installing dependencies ...` → 安装 `requirements.txt` 列出的依赖（约 2 分钟）
- `首次启动: 自动训练标准模型 (~30 秒)` → `app.py` 检测到 `models/current/` 不存在时自动训练一个 standard 模型

完成后会自动跳到你的 app 主页面。

> **注意**：`requirements.txt` 默认 **没有** `sentence-transformers` 与 `torch`（避免每次冷启动 5-10 分钟、超内存）。加强模型按需在 UI 中安装（步骤 6）。

---

## 步骤 5：访问与分享

部署成功后，你的 app 永久挂在：

```
https://<你填的 App URL>.streamlit.app
```

把这个链接发给同事，他们打开即可使用，无需任何安装。

---

## 步骤 6（可选）：在 UI 里安装并训练加强模型

打开 app 后，主页上有 **「🛠 模型管理」** 折叠面板，展开它：

```
┌────────────────────────────────────────────────────────┐
│ 🛠 模型管理 (训练 / 安装加强模型 / 上传新训练集)   ▼  │
├────────────────────────────────────────────────────────┤
│ 当前模型状态                                          │
│ ┌──────────────────────┬──────────────────────────────┐│
│ │ standard: ✅ 已训练   │ enhanced 依赖: ❌ 未安装    ││
│ │                      │ enhanced 模型: ❌ 未训练    ││
│ │                      │                              ││
│ │                      │ [📦 安装加强模型依赖 (~3-5 分钟)] ← 点这个 ││
│ └──────────────────────┴──────────────────────────────┘│
│                                                        │
│ 📤 上传新训练数据 (与原训练集合并后重训)                │
│ [选择训练数据 Excel] (Browse files)                    │
│ ○ standard      ○ enhanced (依赖装好后才出现)          │
│ [🔁 合并并重训]                                        │
│                                                        │
│ 🌱 半监督自训练 (用工作区默认 5 输入打伪标签 + 重训)    │
│ [🌱 一键自训练]                                        │
└────────────────────────────────────────────────────────┘
```

**操作顺序**：

1. 点 **`📦 安装加强模型依赖`**：
   - 会运行时 `pip install torch (cpu 版) + sentence-transformers`
   - 大约需要 3-5 分钟
   - 安装完成会提示「请按 R 键 / 点右上角 ⋮ → Rerun」
2. 重新加载页面后，状态变为 **`✅ 已安装`**，并出现 **`🧠 训练加强模型`** 按钮
3. 点 **`🧠 训练加强模型`**：
   - 训练 enhanced 变体，约 2 分钟
   - 完成后侧栏的「模型变体」选项里 enhanced 即可使用

---

## 步骤 7（可选）：上传新训练集合并重训

如果你有新的已标注 Excel（格式：含 `事件描述` / `隐患类型` / `分类` 三列，类似 `跑冒滴漏与静电风险专项跟踪.xlsx`），可以：

1. 在「📤 上传新训练数据」区拖入文件
2. 选择重训目标变体（standard 或 enhanced）
3. 点 **`🔁 合并并重训`**

新标注会写入 `data/feedback/labels.parquet`（在 Streamlit Cloud 上是 sandbox 文件，应用重启会丢，但当前 session 内有效），然后立即触发重训。

> 想让标注**长期保留**：把上传的标注数据 commit 到仓库（例如新增到 `data/feedback/labels.parquet` 或直接合并到 `跑冒滴漏与静电风险专项跟踪.xlsx`），下次部署就一直生效。

---

## 步骤 8（可选）：半监督自训练

点 **`🌱 一键自训练`** 会：

1. 用当前模型对仓库自带的 5 个原始 xlsx 打高置信伪标签
2. 写入 `data/feedback/pseudo_labels.parquet`
3. 立即用合并后数据重训当前选中的变体

整个过程约 1-2 分钟，结束后 review 触发数与 hazard accuracy 都会有提升。

---

## 故障排查

### ❗ 页面一直停在 "Deploying…" / 跑不起来

最常见的 3 个原因（按顺序检查）：

1. **部署分支选错了**  
   `main` 分支 **没有** `app.py` / `hazard_pipeline/`，只有原始 Excel。  
   必须部署：
   ```
   Branch = cursor/hazard-classifier-pipeline-3238
   Main file path = app.py
   ```
   在 https://share.streamlit.io → 你的 app → 右上角 `⋮` → **Settings** → **General** → 改 Branch → **Save** → 再点 **Reboot app**.

2. **看日志确认卡在哪**  
   同一页面点 **Manage app**（或右下角 "Manage app"）→ **Logs**：
   - `Installing dependencies…` 超过 10 分钟 → 点 **Reboot**
   - `Out of memory` / `Killed` → 已修复（启动时不再自动重训）；确保仓库里有 `models/current/`，然后 Reboot
   - `Error: No such file: app.py` → 分支错了，见上一条

3. **被登录墙挡住**（看起来像一直转圈）  
   打开 https://hazard-classifier.streamlit.app 若跳到 `share.streamlit.io/-/auth/...`，说明开了访问控制。  
   去 Settings → **Sharing** → 关掉 "Only my workspace" / Viewer authentication，改成 **Public**，保存后再开。

### 其他

| 现象 | 原因 / 解决 |
|---|---|
| 部署日志卡在 `Installing dependencies` 超过 10 分钟 | Streamlit Cloud 偶尔慢，点页面右上角 `⋮ → Reboot app` 重新部署 |
| 应用显示 `OOM` 或 `Out of memory` | 加强模型在免费 1GB 内存上偶发；改用 standard 即可（仓库已预置 `models/current/`） |
| 点了「📦 安装加强模型依赖」失败 | 在 `requirements.txt` 中取消注释下面两行后重新部署：<br>`# sentence-transformers>=2.2`<br>`# torch>=2.0` |
| 上传的训练 Excel 没解析到数据 | 检查表头里是否有 `事件描述` 与 `隐患类型` 两列（不能写成 `事件_描述` 这种） |
| App 刷新后人工反馈没了 | Streamlit Cloud sandbox 不持久；要长期保留标注，请 commit 到 git |
| 想换分支部署 | 在 Streamlit Cloud 后台点 `Settings → Branch`，改成 `cursor/hazard-classifier-pipeline-3238` 后 `Save` |

---

## 卸载 / 删除

进入 Streamlit Cloud 后台 → 找到你的 app → 右侧 **`⋮ → Delete app`**。
仓库本身不受影响。

---

附：**App URL 模板**

部署成功后你的访问地址会是：

```
https://<your-subdomain>.streamlit.app
```

例如填的是 `hazard-classifier`，最终就是 `https://hazard-classifier.streamlit.app`。
