# Windows 运行说明

目标：在 Windows 上**解压即用**——离线建 venv、启动 Web 服务、用 CLI 采集真实磁盘。要求 Windows 10/11 x64 + Python 3.10（`py -3 --version` 可确认）。

## 快速开始

1. 解压 `cold-manifest-<版本>-win.zip` 到任意目录（**路径不要含中文或空格**，且尽量短，见下文长路径说明）。
2. 双击或运行 `packaging\windows\setup.cmd` —— 在包根创建 `.venv-win` 并从 `wheels\` 离线安装依赖，最后打印 `cldm <版本>`。
3. 运行 `packaging\windows\start.cmd` —— 默认数据根为包根下 `data\`（可用环境变量 `CLDM_DATA_ROOT` 覆盖），服务监听 `http://0.0.0.0:8765`。
4. 浏览器打开 <http://localhost:8765>；局域网其他机器用 `http://<本机IP>:8765` 访问。

首次启动时 Windows 防火墙会弹窗询问是否允许 Python 监听网络端口——点「允许访问」（仅专用网络即可）。

## 为什么建议以管理员身份启动

**SMART 采集（磁盘健康：温度、通电时间、重映射扇区等）在 Windows 上分两类：**

- **NVMe 盘（M.2 固态）**：普通权限即可读取；
- **USB 桥接盘 / SATA 盘（走 `\\.\PhysicalDriveN` 打开）**：**必须管理员权限**，否则 smartctl 报 `Open failed, Error=5`（拒绝访问），SMART 采集失败。

服务如果以普通用户启动，就会出现「NVMe 盘 SMART 正常、USB 机械盘读不到」的现象——这不是盘坏了，是权限不够。`start.cmd` 启动时会检测管理员权限：非管理员会给出醒目警告，并询问是否**以管理员身份重新启动**（弹出 UAC 确认框）；`cldm.cmd` 非管理员运行时也会打印警告。

排查单个盘的 SMART 可直接在管理员终端执行（`-d sat` 是 USB 桥最常用的通路）：

```bat
smartctl --scan -j
smartctl -i -H -A -j -d sat /dev/sdN
```

## CLI 采集示例

```bat
:: 采集 D 盘整卷；USB 桥接盘拿不到序列号时用 --serial 手填
packaging\windows\cldm.cmd collect D:\ --serial <盘序列号>

:: 只扫某个目录（测试）
packaging\windows\cldm.cmd collect D:\photo-archive --data-root D:\cldm-data --no-smartctl
```

采集完成后，Web 页面（快照页）即可看到该卷。

## 数据根约定

- 默认数据根为**包根下的 `data\`**。`start.cmd` 与 `cldm.cmd` 在 `CLDM_DATA_ROOT` 未设置时都会使用它，因此 Web 服务采集的快照，直接在包根跑 CLI 就能看到，无需额外参数。
- 所有 CLI 子命令的 `--data-root` 也遵循同一优先级：**显式 `--data-root` 参数 > 环境变量 `CLDM_DATA_ROOT` > `./data`**。
- 若你在其他目录执行 CLI（此时默认 `.\data` 与服务的数据根不同），请加 `--data-root <包根>\data`，或先 `set CLDM_DATA_ROOT=<包根>\data`。
- 「快照未注册」类报错会显示实际使用的数据根——若与 Web 服务的不一致，按上面方式指定即可。

其他常用命令（在包根执行；`packaging\windows\cldm.cmd <子命令> --help` 可看全部子命令）：

```bat
:: 补算完整哈希（重复文件报告、内容级对比的前提）
packaging\windows\cldm.cmd hash VOL_P0\20260101T000000Z --policy full

:: 重复文件报告（默认只统计 ≥1MiB；--min-size 0 看全部；--html 另存 HTML）
packaging\windows\cldm.cmd duplicates VOL_P0\20260101T000000Z --min-size 0 --html dup.html

:: 校验盘上副本与源文件完整性（--sample N 抽检 N 个已哈希文件；--full 全量）
packaging\windows\cldm.cmd verify-copy VOL_P0\20260101T000000Z --sample 200

:: 导出 CSV（默认写到**当前目录**，用 --output 指定路径）
packaging\windows\cldm.cmd export VOL_P0\20260101T000000Z --format csv --output snap.csv

:: 服务报"database disk image is malformed"时先自查（只读，不改任何数据）
:: 退出码 0=正常（多半是"假损坏"，重启服务即可）；1=真损坏，重新采集
packaging\windows\cldm.cmd integrity-check VOL_P0\20260101T000000Z
packaging\windows\cldm.cmd integrity-check --all

:: 给磁盘/分区起速记名（Web 对比历史等处会显示昵称；省略名字=清除）
packaging\windows\cldm.cmd nickname disk <磁盘序列号> 仓库盘
packaging\windows\cldm.cmd nickname volume <SN>_P1 备份分区
```

> `integrity-check` 与 `nickname` 都作用于 catalog/快照库所在的数据根：在包根执行（用包根 `data\`）即可与 Web 服务看到同一份数据；在其他目录跑才需要 `--data-root`。昵称只存在 catalog 里，重新采集不会覆盖。

> 写互斥：同一 data_root 同时只允许一个写者（采集/清扫）。并发执行会提示"data_root 被占用"，等待对方完成后重试；进程崩溃锁自动释放。
> 断点续采：大卷采集被中断（崩溃/取消/断电）后，用 `collect --resume` 续接该卷最新的未封库采集，已完成部分自动跳过，结果与一次完整扫描一致。

## 注意事项

- **防火墙**：首次 `start.cmd` 会触发防火墙放行提示；拒绝的话局域网访问不通，可在「Windows 安全中心 → 防火墙 → 允许应用通过防火墙」里补放行 Python。
- **USB 桥盘序列号**：部分 USB/桥接盒会向系统上报桥芯片而非磁盘序列号。`collect` 的输出会标注 `serial_source`；若为 `manual` 之外的值且明显是桥型号，请加 `--serial <盘序列号>`（盘身标签或厂商工具可查）。
- **长路径（>260 字符）**：Windows 默认 `MAX_PATH` 260。深目录树可能采集不全。建议开启系统长路径：注册表 `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` 后重启；Python 3.6+ 会自动跟随该开关。若不能改注册表，尽量把包和 data 根放在短路径下（如 `C:\cldm\`）。
- **数据根与盘上副本**：数据根（默认 `data\`）存放快照 SQLite 库与 catalog；每份快照会同时在**被采集盘上**写一份 `_coldmanifest` 副本目录（含封库库与 sha256 旁车 json），作为随盘冷备。请确保目标盘有少量剩余空间（约为该卷元数据量，通常几百 MB 以内）。
- **smartctl**：默认尝试调用 smartctl 读取 SMART/真实序列号；未安装时自动降级（`smart_status=unavailable`），不影响采集，也可 `--no-smartctl` 显式跳过。
- **SMART 与 USB 桥（设备名自动映射，本机 USB 盘实测）**：Windows 上 smartctl 对 USB 桥盘直接用 `\\.\PhysicalDriveN` 常报 `Invalid argument`，而 `smartctl --scan` 给出的 `/dev/sdN` + `-d sat` 能读到真盘身份。工具已自动处理（**设备名自动映射**）：先跑 `smartctl --scan -j`（JSON 优先、纯文本回退）建立"磁盘编号 → 设备名"映射，然后按候选链（环境变量覆盖 → 扫描匹配 → `/dev/sdN` → `\\.\PhysicalDriveN` → 盘符）**逐个尝试**，每个候选再走类型兜底链（自动 → `-d sat` → `-d sat,12` → 扫描建议 → 常见桥芯片专用参数；`-d sat` 即"通过 USB 桥按 SATA 协议透传读 SMART"）。磁盘页「现在读取 SMART」与采集前检查同样走这条完整候选链（并带容量软校验：扫描映射的设备实际容量与盘差超 20% 会被剔除，防映射错位）。映射结论（扫描到的设备表、候选链、实际生效设备）会落采集 meta 的 `smart_scan_info_json`。个别桥仍读不到时可用环境变量兜底：`CLDM_SMARTCTL_DEVICE`（直接指定设备串，如 `/dev/sdc`）与 `CLDM_SMARTCTL_ARGS`（额外透传参数）——**改环境变量后要重启服务才生效**。全部尝试都会自动进行、无需手动指定：读不到时采集 meta 里记 `smart_attempts_json`（每次尝试的设备串、参数与报错摘要），磁盘页「现在读取 SMART」也会显示失败原因（权限不足请以管理员运行；设备打不开通常是该用 `/dev/sdN` 形态；参数/设备类型问题时检查 `-d` 写法）。`-d` 桥专用参数的合法写法：`usbjmicron` / `usbsunplus` / `usbprolific` 可裸用；JMicron 56x 芯片必须写成 `jms56x,N[,…]` 形式（最小合法形式 `jms56x,0`），裸写 `jms56x` 是非法参数。
- **假损坏自查**：若 Web 页面报"快照库读取失败，可能被并发写入影响"，通常是"假损坏"（文件没坏，重启服务即恢复）；可先 `cldm.cmd integrity-check <快照ID>` 确认——退出码 0 就放心重启，退出码 1 才是真损坏需重新采集。
- **编码**：控制台输出为 UTF-8；如出现乱码，先执行 `chcp 65001`。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `setup.cmd` 报 py 不是内部命令 | 未安装 Python 3.10+ 或未勾选 launcher，去 <https://www.python.org> 安装 |
| 离线安装失败 | wheels\ 是否完整；Python 是否恰为 3.10（wheels 按 cp310 构建）；或联网回退 `.venv-win\Scripts\python -m pip install -e .` |
| 端口 8765 被占用 | `start.cmd` 中改端口，或设 `CLDM_DATA_ROOT` 后手动运行 `cldm serve --port 8766` |
