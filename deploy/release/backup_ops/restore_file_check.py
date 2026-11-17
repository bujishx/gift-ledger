"""Read-only consistency check of three public S011 recovery input files.

This does not decrypt, read an identity, connect to a NAS, or restore a database.
Keep the matching frozen backup_store.py beside this standalone module.
"""

import argparse
import json
import os
import re
from pathlib import Path, PurePosixPath

import backup_store

TABLES = {
    "alembic_version", "users", "contacts", "event_records", "gift_records",
    "legacy_import_batches", "relationship_types", "kinship_types", "event_types",
}
MAX_APP_FILES = 4096


class CheckError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise CheckError(code)


def _local_file(value):
    raw = os.fspath(value)
    require(not raw.startswith(("\\\\", "//")), "local_file_required")
    supplied = Path(raw)
    require(".." not in supplied.parts, "path_traversal_rejected")
    path = Path(os.path.abspath(raw))
    backup_store._path(path.parent)
    backup_store._metadata(path)
    return path


def _signature(info):
    return backup_store._signature(info) + (info.st_ctime_ns,)


def _snapshot(paths):
    files, parents = {}, {}
    for path in paths:
        files[path] = _signature(backup_store._metadata(path))
        for parent in path.parents:
            info = backup_store._metadata(parent, directory=True)
            # Unrelated downloads may change a directory's mtime or size.
            parents[parent] = (info.st_dev, info.st_ino, info.st_mode,
                               getattr(info, "st_file_attributes", 0))
    return files, parents


def receipt(path, action):
    """Accept only a standard action alias or its correct historical basename."""
    path = _local_file(path)
    value = backup_store._json(path)
    operation = value.get("operation_id")
    require(isinstance(operation, str) and backup_store.ID.fullmatch(operation),
            "operation_id_invalid")
    require(path.name in {action + ".json", "completed-" + operation + ".json"},
            "receipt_filename_mismatch")
    return backup_store._receipt(value, action)


def _recovery_content(backup):
    details = backup["details"]
    fingerprint = details["fingerprint"]
    require(isinstance(fingerprint, dict)
            and set(fingerprint) == {"algorithm", "schema_revision", "tables"}
            and fingerprint["algorithm"] == "sha256-canonical-v1"
            and fingerprint["schema_revision"] == "schema_v2_s011"
            and details["schema_revision"] == "schema_v2_s011"
            and isinstance(fingerprint["tables"], dict)
            and set(fingerprint["tables"]) == TABLES,
            "s011_full_table_fingerprint_required")
    for item in fingerprint["tables"].values():
        require(isinstance(item, dict) and set(item) == {"rows", "sha256"}
                and type(item["rows"]) is int and item["rows"] >= 0
                and isinstance(item["sha256"], str)
                and backup_store.HASH.fullmatch(item["sha256"]),
                "table_fingerprint_invalid")
    app_files = details["app_files"]
    require(isinstance(app_files, dict) and len(app_files) <= MAX_APP_FILES,
            "application_file_metadata_invalid")
    for name, item in app_files.items():
        require(isinstance(name, str) and 0 < len(name) <= 240
                and "\\" not in name and ":" not in name
                and name.startswith("app-data/")
                and not PurePosixPath(name).is_absolute()
                and all(part not in {"", ".", ".."}
                        and not part.endswith((".", " "))
                        and re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                        for part in name.split("/"))
                and isinstance(item, dict) and set(item) == {"size", "sha256"}
                and type(item["size"]) is int and item["size"] >= 0
                and isinstance(item["sha256"], str)
                and backup_store.HASH.fullmatch(item["sha256"]),
                "application_file_metadata_invalid")


def check(ciphertext, backup_path, verify_path):
    """Check exactly the three chosen public inputs without changing any file."""
    try:
        paths = tuple(_local_file(value) for value in
                      (ciphertext, backup_path, verify_path))
        require(len(set(paths)) == 3, "three_distinct_inputs_required")
        before = _snapshot(paths)
        cipher, backup_file, verify_file = paths
        backup, verification = backup_store._pair(
            receipt(backup_file, "backup"), receipt(verify_file, "verify")
        )
        _recovery_content(backup)
        require(re.fullmatch(r"backup-[0-9a-f]{32}\.glgui\.age", cipher.name)
                and cipher.name == backup["details"]["ciphertext"],
                "ciphertext_filename_mismatch")
        actual = backup_store._read_file(cipher, ciphertext=True)
        require(actual == backup["details"]["ciphertext_sha256"],
                "ciphertext_hash_mismatch")
        require(_snapshot(paths) == before, "inputs_changed_during_check")
        return {
            "format": "gift-ledger-three-file-recovery-check-v1",
            "success": True,
            "local_ciphertext_sha256_matches": True,
            "verify_receipt_matches": True,
            "full_content_metadata_matches": True,
            "sha256": actual,
            "bytes": before[0][cipher][2],
            "backup_operation_id": backup["operation_id"],
            "verify_operation_id": verification["operation_id"],
            "decryption_verified": False,
            "restoration_verified": False,
            "private_key_verified": False,
            "source_authenticity_verified": False,
            "nas_accessed": False,
        }
    except backup_store.BackupStoreError as error:
        raise CheckError(error.code) from None
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, CheckError):
            raise
        raise CheckError("input_unavailable_or_invalid") from None


def _message(result):
    return ("三份本机文件的一致性核对通过。\n"
            "已读取密文实际字节并核对 SHA256，以及备份/核验记录的来源、编号和完整内容摘要。\n"
            "密文大小：{bytes} 字节\nSHA256：{sha256}\n\n"
            "本次未解密、未验证私钥、未恢复网站，也未连接 NAS。\n"
            "公开记录的一致性不能替代独立保留的来源证据及实际恢复验收。").format(**result)


def _error_message(error):
    return ("三文件核对未通过，请保留原文件并核对是否混用了不同轮次资料。\n"
            "不要通过改文件名或编辑 JSON 绕过检查。\n检查原因：" + error.code)


def gui():
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    window = tk.Tk()
    window.title("往来有礼 · 恢复三文件核对")
    window.geometry("680x330")
    window.resizable(False, False)
    ttk.Label(window, padding=18, wraplength=630, text=(
        "依次选择同一轮备份的三个数据文件：\n"
        "原 .glgui.age 密文、backup.json、verify.json。\n"
        "也支持各自正确的旧 completed-编号.json。\n\n"
        "本工具只读核对实际密文摘要和公开记录；不读取私钥、不解密、不恢复网站。"
    )).pack(anchor="w")
    status = tk.StringVar(value="点击下方按钮开始；取消文件选择会返回本窗口。")
    ttk.Label(window, textvariable=status, padding=18, wraplength=630).pack(anchor="w")
    busy = [False]

    def finish(result=None, error=None, cancelled=False):
        busy[0] = False
        button.configure(state="normal")
        if cancelled:
            status.set("已取消选择，原文件保持不变。")
        elif error is not None:
            status.set("核对未通过：" + error.code)
            messagebox.showerror("核对未通过", _error_message(error), parent=window)
        else:
            status.set("三文件核对通过；尚未解密、验证私钥或恢复网站。")
            messagebox.showinfo("三文件核对通过", _message(result), parent=window)

    def choose():
        if busy[0]:
            return
        busy[0] = True
        button.configure(state="disabled")
        cipher = filedialog.askopenfilename(
            title="选择原 backup-编号.glgui.age 密文",
            filetypes=[("备份密文", "*.glgui.age")], parent=window)
        if not cipher:
            finish(cancelled=True)
            return
        backup = filedialog.askopenfilename(
            title="选择 backup.json 或备份 completed-编号.json（action=backup）",
            filetypes=[("公开完成记录", "*.json")], parent=window)
        if not backup:
            finish(cancelled=True)
            return
        verify = filedialog.askopenfilename(
            title="选择 verify.json 或核验 completed-编号.json（action=verify）",
            filetypes=[("公开完成记录", "*.json")], parent=window)
        if not verify:
            finish(cancelled=True)
            return
        status.set("正在读取实际密文并核对三文件，请等待……")

        def work():
            try:
                result = check(cipher, backup, verify)
                window.after(0, finish, result)
            except CheckError as error:
                window.after(0, finish, None, error)
            except Exception:
                window.after(0, finish, None, CheckError("unexpected_check_error"))
        threading.Thread(target=work, daemon=True).start()

    button = ttk.Button(window, text="选择三文件并核对", command=choose)
    button.pack(pady=5)

    def close():
        if busy[0]:
            messagebox.showinfo("核对进行中", "请先取消文件选择，或等待本次核对结束。", parent=window)
        else:
            window.destroy()
    window.protocol("WM_DELETE_WINDOW", close)
    window.mainloop()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读核对三个公开恢复输入文件；不解密、不恢复。")
    parser.add_argument("--ciphertext")
    parser.add_argument("--backup")
    parser.add_argument("--verify")
    parser.add_argument("--human", action="store_true")
    args = parser.parse_args(argv)
    if not any((args.ciphertext, args.backup, args.verify, args.human)):
        return gui()
    if not all((args.ciphertext, args.backup, args.verify)):
        parser.error("命令行核对必须同时提供 --ciphertext、--backup 和 --verify。")
    try:
        result = check(args.ciphertext, args.backup, args.verify)
    except CheckError as error:
        print(_error_message(error) if args.human else
              json.dumps({"success": False, "error": error.code}, ensure_ascii=False))
        return 1
    print(_message(result) if args.human else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
