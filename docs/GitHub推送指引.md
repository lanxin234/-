# GitHub 推送指引

本地仓库已经准备好了：**15 个文件、1 次提交、分支 `main`**。
你只需要在 GitHub 网页建好仓库，然后执行 3 条命令。

---

## 第 1 步：在 GitHub 网页创建仓库

1. 打开 https://github.com/new
2. **Repository name** 填一个名字，推荐 `blockchain-secondhand`
3. **Description**（可选）填：基于区块链的二手交易平台 · Solidity + Flask
4. 选择 **Public**（公开）或 **Private**（私有），都行
5. ⚠️ **不要勾选** 这三项：
   - ❌ Add a README file
   - ❌ Add .gitignore
   - ❌ Choose a license

   **原因**：本地已经有这些文件了。如果网页这边也生成一份，push 时会因为"历史不相关"被拒绝。
6. 点 **Create repository**

创建后页面会显示仓库地址，形如：
```
https://github.com/<你的用户名>/blockchain-secondhand.git
```

---

## 第 2 步：执行推送命令

打开终端，**逐条**执行（把 `<你的用户名>` 和 `<仓库名>` 换成实际的）：

```bash
cd C:/Users/25191/Desktop/xiangmu

# 关联远程仓库
git remote add origin https://github.com/<你的用户名>/<仓库名>.git

# 推送
git push -u origin main
```

### 弹出登录框怎么办？

Git for Windows 会弹窗要求认证。**密码不能填你的 GitHub 登录密码**（GitHub 早就禁用了密码推送），必须用 **Personal Access Token**：

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

浏览器打开 `https://github.com/<你的用户名>/<仓库名>`，应该能看到 15 个文件和完整的 README。

---

## 常见报错

| 报错 | 原因 | 解决 |
|---|---|---|
| `remote origin already exists` | 已经加过远程了 | 改用 `git remote set-url origin <地址>` |
| `Updates were rejected` | 网页建仓库时勾了 README | `git pull --rebase origin main` 后再 push |
| `Authentication failed` | 用了登录密码而非 token | 按上面步骤生成 token |
| `Support for password authentication was removed` | 同上 | 同上 |
| `src refspec main does not match any` | 分支名不对 | `git branch -M main` 后再 push |
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
git remote set-url origin git@github.com:<你的用户名>/<仓库名>.git
```

---

## 当前仓库状态（供参考）

```
提交：ba83417  feat: 基于区块链的二手交易平台（链易）
分支：main
文件：15 个 / 8280 行
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
