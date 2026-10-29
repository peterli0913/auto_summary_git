# 在 Render 上部署隐患分类 App（图文步骤）

免费、比 Streamlit Cloud 更稳；休眠后第一次打开会慢 30–60 秒（冷启动）。

仓库地址：`https://github.com/peterli0913/auto_summary_git`  
**必须用分支** `cursor/hazard-classifier-pipeline-3238`（`main` 没有 `app.py`）。

---

## 准备

- GitHub 账号能访问这个仓库
- 打开 https://render.com → 用 GitHub 登录并授权

---

## 方式 A：用 Blueprint 一键部署（推荐）

仓库里已有 `render.yaml`。

1. 打开 https://dashboard.render.com  
2. 点 **New +** → **Blueprint**  
3. 选仓库 `peterli0913/auto_summary_git`  
4. 若提示选分支，选 **`cursor/hazard-classifier-pipeline-3238`**  
5. Render 会读到 `render.yaml`，服务名一般是 `hazard-classifier`  
6. 点 **Apply** / **Deploy Blueprint**

等 3–8 分钟（装依赖）。成功后会给你一个地址，类似：

```
https://hazard-classifier.onrender.com
```

---

## 方式 B：手动创建 Web Service（同样简单）

如果 Blueprint 不好用，按这个做：

1. https://dashboard.render.com → **New +** → **Web Service**
2. 连接 GitHub → 选 `peterli0913/auto_summary_git`
3. 填下面这些：

| 字段 | 填什么 |
|---|---|
| **Name** | `hazard-classifier`（随便起） |
| **Region** | 选离你近的（如 Singapore / Oregon） |
| **Branch** | `cursor/hazard-classifier-pipeline-3238` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false` |
| **Instance Type** | **Free** |

4. 点 **Advanced** → 加环境变量（可选但建议）：

| Key | Value |
|---|---|
| `PYTHON_VERSION` | `3.12.3` |

5. 点 **Create Web Service**

---

## 部署成功后怎么用

浏览器打开 Render 给你的 URL，例如：

```
https://hazard-classifier.onrender.com
```

界面和之前 Streamlit Cloud 一样：
- 侧栏选 **standard**（默认，仓库已带模型，秒开）
- 上传 5 个 Excel → 汇总并分类 → 下载结果
- 要用 **enhanced**：展开「🛠 模型管理」→ 安装依赖 → 再训练/加载（免费机内存小，可能装不动，建议日常用 standard）

---

## 免费档注意事项

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 第一次打开等很久 | Free 服务休眠后冷启动 | 等 30–60 秒；可加 Render 付费免休眠 |
| Build 失败 / OOM | 免费构建内存有限 | 确认没装 torch；`requirements.txt` 里 enhanced 依赖保持注释 |
| 404 / 应用没起来 | 分支选成了 `main` | Settings → 改 Branch 为 `cursor/hazard-classifier-pipeline-3238` → Manual Deploy |
| 端口错误 | Start Command 没带 `$PORT` | 用上面那条 Start Command 原样复制 |

---

## 和 Streamlit Cloud 对比

| | Streamlit Cloud | Render Free |
|---|---|---|
| 地址形态 | `xxx.streamlit.app` | `xxx.onrender.com` |
| 限流/Throttled | 常见 | 较少（主要是休眠） |
| 冷启动 | 有时有 | 休眠后约 30–60 秒 |
| 适合 | 快速试用 | 更稳的长期免费挂机 |

---

## 本地验证 Start Command（可选）

```bash
export PORT=8501
streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
```

浏览器打开 http://localhost:8501 能用，Render 上一般也能用。

---

## 卸载

Render Dashboard → 你的服务 → **Settings** → 拉到最下 → **Delete Web Service**。
