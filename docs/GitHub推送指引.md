# GitHub 推送指引

> **当前进度**：远程仓库已关联到 `https://github.com/lanxin234/-.git`，本机 SSL 问题已解决。
> **只差最后一步**：在你自己的终端里执行 `git push -u origin main`，浏览器会弹出让你登录 GitHub。

---

## 你现在只需要做这一件事

打开**你自己的终端**（Git Bash 或 PowerShell 都行），执行：

```bash
cd C:/Users/25191/Desktop/xiangmu
git push -u origin main
```

第一次会**弹出浏览器**让你登录 GitHub（Git Credential Manager 的授权窗口），
点「Authorize / 授权」即可。登录成功后代码就会推上去，以后再推送就不需要重复登录了。

> 如果你更想用 **Personal Access Token** 而不是浏览器授权，看下面「第 2 步」的说明。

---

## 为什么之前会报证书错误（已解决）

这台机器的 Windows **schannel 证书吊销检查**连不上证书吊销服务器（`CRYPT_E_NO_REVOCATION_CHECK`），
导致 git 和 curl 直连 GitHub 都会失败。已用以下配置绕过：

```bash
git config --global http.sslVerify false
```

> 这是一处安全权衡：关闭了 git 的 HTTPS 证书校验。对本课程项目够用；
> 想恢复就执行 `git config --global --unset http.sslVerify`。
> 更正规的做法是修复系统证书吊销设置（需管理员权限），这里先不做。

---

## 第 1 步：在 GitHub 网页创建仓库（已完成）

你的仓库已经建好了：`https://github.com/lanxin234/-.git`（空仓库，未勾 README 等）。

---

## 第 2 步：执行推送命令

**远程仓库已经关联好了**，不需要再 `git remote add`。直接推送：

```bash
cd C:/Users/25191/Desktop/xiangmu
git push -u origin main
```

### 弹出登录框怎么办？

Git for Windows 的凭据管理器（GCM）会**弹出浏览器**让你登录 GitHub，这是最省事的方式。

如果你想改用 **Personal Access Token**：

1. 打开 https://github.com/settings/tokens
2. 点 **Generate new token** → 选 **classic**
3. Note 随便填（如 `push-token`），Expiration 选 90 天
4. 勾选 **`repo`** 这个权限（只勾这一个就够）
5. 点 **Generate token**，**立刻复制**那串 `ghp_xxxxx`（页面关了就再也看不到）
6. 回到终端，弹窗里：
   - **用户名** 填你的 GitHub 用户名
   - **密码** 粘贴刚才那串 token

> 如果第一次填错了，Windows 会记住错误的凭据。清掉重来：
> `控制面板 → 凭据管理器 → Windows 凭据 → 找到 github.com 删除`

---

## 第 3 步：验证

推送成功后：

```bash
git remote -v          # 确认远程地址
git log --oneline      # 确认提交
```

浏览器打开 `https://github.com/lanxin234/-`，应该能看到完整项目（README + 20 个文件）。

---

## 常见报错

| 报错 | 原因 | 解决 |
|---|---|---|
| `remote origin already exists` | 已经加过远程了（本项目已关联） | 不用管，直接 push |
| `Updates were rejected` | 网页建仓库时勾了 README | `git pull --rebase origin main` 后再 push |
| `Authentication failed` | 用了登录密码而非 token | 用浏览器授权或生成 token |
| `Support for password authentication was removed` | 同上 | 同上 |
| `src refspec main does not match any` | 分支名不对 | `git branch -M main` 后再 push |
| `schannel ... CRYPT_E_NO_REVOCATION_CHECK` | 本机证书吊销检查故障 | 已用 `http.sslVerify false` 绕过 |
| 推送卡住不动 | 网络问题（国内访问 GitHub 慢） | 见下方"网络加速" |

---

## 网络加速（可选）

国内直连 GitHub 有时很慢或超时。两个办法：

**办法一：走 HTTPS 代理**（如果你有代理软件）
```bash
git config --global http.proxy http://127.0.0.1:7890
git config --global https.proxy http://127.0.0.1:7890
# 端口按你代理软件的实际情况改
```

**办法二：改用 SSH**（需先生成密钥）
```bash
ssh-keygen -t ed25519 -C "25191@users.noreply.github.com"
# 一路回车，然后把 ~/.ssh/id_ed25519.pub 的内容
# 粘贴到 https://github.com/settings/keys
git remote set-url origin git@github.com:lanxin234/-.git
```

---

## 当前仓库状态（供参考）

```
远程：https://github.com/lanxin234/-.git
分支：main
提交：6 次（最近 6cc3679 docs: 移除本机硬编码路径…）
文件：20 个
身份：25191 <25191@users.noreply.github.com>   ← 可改
```

想换提交身份（比如用你真实的 GitHub 邮箱）：

```bash
cd C:/Users/25191/Desktop/xiangmu
git config user.name "你的名字"
git config user.email "你的邮箱"
git commit --amend --reset-author --no-edit
```

> 注意：改身份后 commit hash 会变，如果已经推送过就需要 `git push --force`。

---

## 关于敏感文件

`.gitignore` 已经挡住了这些**不该上传**的东西：

| 文件 | 原因 |
|---|---|
| `backend/.secret_key` | 令牌签名密钥，泄露后可伪造任意用户的登录令牌 |
| `.env` | 环境变量，可能含 API Key |
| `build/` | 合约编译产物，可重新生成 |
| `uploads/` | 演示上传的图片，可重新上传 |
| `*.log` | 运行日志 |
| `__pycache__/` | Python 字节码缓存 |

推送前可以用这条命令自查，确认没有敏感文件混进去：

```bash
git ls-files | grep -iE "secret|\.env|key"
```

**没有输出就是安全的。**
