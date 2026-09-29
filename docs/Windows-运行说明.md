# Windows 运行说明

目标：在 Windows 上**解压即用**——离线建 venv、启动 Web 服务、用 CLI 采集真实磁盘。要求 Windows 10/11 x64 + Python 3.10（`py -3 --version` 可确认）。

## 快速开始

1. 解压 `cold-manifest-<版本>-win.zip` 到任意目录（**路径不要含中文或空格**，且尽量短，见下文长路径说明）。
2. 双击或运行 `packaging\windows\setup.cmd` —— 在包根创建 `.venv-win` 并从 `wheels\` 离线安装依赖，最后打印 `cldm <版本>`。
3. 运行 `packaging\windows\start.cmd` —— 默认数据根为包根下 `data\`（可用环境变量 `CLDM_DATA_ROOT` 覆盖），服务监听 `http://0.0.0.0:8765`。
4. 浏览器打开 <http://localhost:8765>；局域网其他机器用 `http://<本机IP>:8765` 访问。

首次启动时 Windows 防火墙会弹窗询问是否允许 Python 监听网络端口——点「允许访问」（仅专用网络即可）。

## CLI 采集示例

```bat
:: 采集 D 盘整卷；USB 桥接盘拿不到序列号时用 --serial 手填
packaging\windows\cldm.cmd collect D:\ --serial <盘序列号>

:: 只扫某个目录（测试）
packaging\windows\cldm.cmd collect D:\photo-archive --data-root D:\cldm-data --no-smartctl
```

采集完成后，Web 页面（快照页）即可看到该卷。

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
```

> 写互斥：同一 data_root 同时只允许一个写者（采集/清扫）。并发执行会提示"data_root 被占用"，等待对方完成后重试；进程崩溃锁自动释放。
> 断点续采：大卷采集被中断（崩溃/取消/断电）后，用 `collect --resume` 续接该卷最新的未封库采集，已完成部分自动跳过，结果与一次完整扫描一致。

## 注意事项

- **防火墙**：首次 `start.cmd` 会触发防火墙放行提示；拒绝的话局域网访问不通，可在「Windows 安全中心 → 防火墙 → 允许应用通过防火墙」里补放行 Python。
- **USB 桥盘序列号**：部分 USB/桥接盒会向系统上报桥芯片而非磁盘序列号。`collect` 的输出会标注 `serial_source`；若为 `manual` 之外的值且明显是桥型号，请加 `--serial <盘序列号>`（盘身标签或厂商工具可查）。
- **长路径（>260 字符）**：Windows 默认 `MAX_PATH` 260。深目录树可能采集不全。建议开启系统长路径：注册表 `HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled = 1` 后重启；Python 3.6+ 会自动跟随该开关。若不能改注册表，尽量把包和 data 根放在短路径下（如 `C:\cldm\`）。
- **数据根与盘上副本**：数据根（默认 `data\`）存放快照 SQLite 库与 catalog；每份快照会同时在**被采集盘上**写一份 `_coldmanifest` 副本目录（含封库库与 sha256 旁车 json），作为随盘冷备。请确保目标盘有少量剩余空间（约为该卷元数据量，通常几百 MB 以内）。
- **smartctl**：默认尝试调用 smartctl 读取 SMART/真实序列号；未安装时自动降级（`smart_status=unavailable`），不影响采集，也可 `--no-smartctl` 显式跳过。
- **编码**：控制台输出为 UTF-8；如出现乱码，先执行 `chcp 65001`。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `setup.cmd` 报 py 不是内部命令 | 未安装 Python 3.10+ 或未勾选 launcher，去 <https://www.python.org> 安装 |
| 离线安装失败 | wheels\ 是否完整；Python 是否恰为 3.10（wheels 按 cp310 构建）；或联网回退 `.venv-win\Scripts\python -m pip install -e .` |
| 端口 8765 被占用 | `start.cmd` 中改端口，或设 `CLDM_DATA_ROOT` 后手动运行 `cldm serve --port 8766` |
