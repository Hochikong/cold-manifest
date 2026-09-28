"""采集总控：串联元数据采集与目录树扫描。"""

import csv
import logging
import os

from backuptools.disk import collect_metadata
from backuptools.scanner import scan_tree
from backuptools.csvschema import METADATA_HEADER, make_metadata_row, make_warning_row
from backuptools.utils import utc_now_iso

logger = logging.getLogger(__name__)


def collect(
    drive: str,
    output_dir: str,
    serial: str = None,
    exclude_globs: list = None,
    exclude_hidden: bool = False,
    include_system: bool = False,
    no_progress: bool = False,
) -> str:
    """采集指定盘符的完整数据快照。

    返回输出目录路径。
    """
    # 采集元数据
    logger.info("Collecting metadata for %s...", drive)
    metadata = collect_metadata(drive, manual_serial=serial)
    volume_id = metadata.get("volume_id", "UNKNOWN")

    # 创建输出目录
    snapshot_dir = os.path.join(output_dir, volume_id)
    os.makedirs(snapshot_dir, exist_ok=True)
    logger.info("Output directory: %s", snapshot_dir)

    # 写入元数据（附加采集时间和版本）
    from backuptools import __version__
    metadata["collect_time_utc"] = utc_now_iso()
    metadata["collector_version"] = __version__

    meta_path = os.path.join(snapshot_dir, "metadata.csv")
    with open(meta_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=METADATA_HEADER)
        writer.writeheader()
        for name in metadata:
            writer.writerow(make_metadata_row(name, metadata[name]))
    logger.info("Metadata written: %s", meta_path)

    # 扫描目录树
    root_path = drive + "\\"
    tree_path = os.path.join(snapshot_dir, "tree.csv")
    warnings_path = os.path.join(snapshot_dir, "warnings.csv")

    # 进度回调
    if not no_progress:
        try:
            from tqdm import tqdm
            pbar = tqdm(desc="Scanning", unit=" dirs")
            def progress_cb(_dirpath, _count):
                pbar.update(1)
            do_progress = progress_cb
        except ImportError:
            do_progress = None
    else:
        do_progress = None

    scan_tree(
        root=root_path,
        volume_id=volume_id,
        output_path=tree_path,
        warnings_path=warnings_path,
        include_system=include_system,
        exclude_hidden=exclude_hidden,
        exclude_globs=exclude_globs,
        progress_callback=do_progress,
    )

    if not no_progress and do_progress is not None:
        try:
            pbar.close()
        except Exception:
            pass

    logger.info("Collection complete: %s", snapshot_dir)
    return snapshot_dir
