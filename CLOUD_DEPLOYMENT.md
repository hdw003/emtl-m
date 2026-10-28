# 云端安装与测试

这是 Python 库和命令行工具，安装后通过 `emt` 使用；不包含常驻 Web 服务。

## 安装

需要 Python 3.12+ 与 uv。在仓库根目录运行：

```sh
uv venv --python python3.12 .venv
uv pip install --python .venv/bin/python -e '.[tests,captcha]'
.venv/bin/emt --version
.venv/bin/python -m pytest
```

`captcha` 使用本地 OCR。首次安装会下载模型和运行库，不需要付费验证码服务。

## 只读联网检查

```sh
.venv/bin/python scripts/cloud_smoke.py
```

该脚本只查询公开行情并读取公开登录页，不调用登录、下单或撤单接口。
退出码 0 表示两项检查都通过，1 表示有检查失败或被网络代理拦截。
通过这些检查不代表账户认证或实盘交易已经验证。

云环境需要通过受支持的网络配置入口允许 HTTPS 访问以下主机：

- `emhsmarketwg.eastmoneysec.com`：行情
- `jywg.18.cn`：交易站

保留运行环境配置的代理和 CA 信任，保持 TLS 证书校验开启。
本分支修复了上游强制忽略环境代理的问题，并添加代理及 CA 配置回归测试。

## 账户功能

后续账户验证需要将 `EM_USERNAME` 和 `EM_PASSWORD` 配置到环境的凭据管理中。
不要将真实凭据写入代码、提交记录或聊天。账户查询示例：

```sh
.venv/bin/emt query asset
```

该命令会执行真实账户登录。当前云端测试没有执行账户查询或交易指令。
