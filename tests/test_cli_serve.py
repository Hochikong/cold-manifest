"""serve 子命令的数据根处理回归测试（曾出现 --data-root 默认值覆盖环境变量的问题）。"""

import os


def test_serve_without_flag_keeps_env_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("CLDM_DATA_ROOT", str(tmp_path))
    import uvicorn

    calls = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.update(k))

    from cold_manifest.cli import main

    assert main(["serve"]) == 0
    assert os.environ["CLDM_DATA_ROOT"] == str(tmp_path)
    assert calls.get("host") == "0.0.0.0"


def test_serve_with_flag_sets_env_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("CLDM_DATA_ROOT", raising=False)
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)

    from cold_manifest.cli import main

    target = tmp_path / "dr"
    assert main(["serve", "--data-root", str(target)]) == 0
    assert os.environ["CLDM_DATA_ROOT"] == str(target.resolve())
