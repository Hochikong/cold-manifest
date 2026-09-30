# Linux 运行说明

cold-manifest Linux 运行包：源码 + 启动脚本，通过 venv 安装后以 Web 服务方式运行。

## 前提

- **Python ≥ 3.10**（含 `python3-venv`；Debian/Ubuntu 缺失时 `sudo apt install python3 python3-venv`）
- 如使用离线包根（带 `wheels-linux/` 目录）则无需联网；否则首次安装需联网

## 安装

```bash
packaging/linux/setup.sh
```

脚本会：
1. 检查 `python3` 版本 ≥ 3.10（过低时清晰报错退出）
2. 在包根创建 `.venv-linux`
3. 安装本包——**包根有 `wheels-linux/` 时完全离线安装**，否则**在线** `pip install -e .`（输出中会说明使用哪种模式）

## 启动 Web 服务

```bash
packaging/linux/start.sh
```

- 默认监听 `0.0.0.0:8765`，用 `CLDM_PORT=8790 packaging/linux/start.sh` 改端口
- 默认数据根 `<包根>/data`，用 `CLDM_DATA_ROOT=/path/to/data` 改

## 数据根约定（优先级）

1. 命令行显式参数 `--data-root`（最高）
2. 环境变量 `CLDM_DATA_ROOT`
3. 默认 `<包根>/data`

三个脚本（`setup.sh` / `start.sh` / `cldm.sh`）行为一致。

## 常用命令（cldm.sh）

`cldm.sh` 是 CLI 包装器，等价于 `.venv-linux/bin/cldm`：

```bash
# 采集一个卷（关闭 smartctl、指定盘序列号）
packaging/linux/cldm.sh collect /media/usb1 --serial WD-XXXX --no-smartctl

# 重复文件报告（阈值 0 = 全部）
packaging/linux/cldm.sh duplicates <快照ID> --min-size 0

# 校验盘上副本完整性
packaging/linux/cldm.sh verify-copy <快照ID>

# 比对两个快照
packaging/linux/cldm.sh diff <快照A> <快照B>

# 启动哈希（默认关闭，按需启用）
packaging/linux/cldm.sh hash <快照ID> --policy full
```

## 端口与防火墙

- 默认端口 **8765**（`CLDM_PORT` 可改）；服务绑定 `0.0.0.0`，局域网内其他机器可访问
- 如需局域网访问，放行端口：
  ```bash
  sudo ufw allow 8765/tcp    # Ubuntu/Debian
  sudo firewall-cmd --add-port=8765/tcp --permanent && sudo firewall-cmd --reload  # RHEL/Fedora
  ```
- 仅本机使用时可改用 `--host 127.0.0.1`（直接调 `.venv-linux/bin/cldm serve`）

## 卸载

删除包根目录即可（venv、数据、快照库均在包根内，除非另行指定了 `CLDM_DATA_ROOT`）。
