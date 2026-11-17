"""Validate the v2 private role layout, then reuse the unchanged S004-S011 tool."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from prepare_volumes import verify_layout


def run(project):
    result = verify_layout(project)
    spec = importlib.util.spec_from_file_location(
        "gui_v2_legacy_initialize", "/opt/gift-ledger-gui/initialize.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SOCKET = Path("/work/mysql/mysqld.sock")
    return module.run(
        project,
        site=Path(result["site_path"]),
        secret_dir=Path(result["secret_bindings_path"]),
        app_data=Path("/work/app-data"),
    )


def main():
    parser = argparse.ArgumentParser(description="私有卷全新初始化；完成重入只读核验。")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()
    try:
        return run(args.project)
    except BaseException:  # noqa: BLE001 -- never expose private driver failures
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": "initialization_stopped_preserve_evidence",
                }
            ),
            file=sys.stderr,
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
