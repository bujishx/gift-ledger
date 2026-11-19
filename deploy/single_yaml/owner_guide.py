"""Render the public UTF-8 owner's notes. Data paths in this file are host paths."""

import base64
import json
from pathlib import PurePosixPath
import re
from shlex import quote
from textwrap import dedent
from urllib.parse import urlsplit


FILENAME = "本站维护说明.txt"
DSM_DOCKER = "/var/packages/ContainerManager/target/usr/bin/docker"
DSM_PYTHON = "/usr/bin/python3"
FIND_PREPARE = dedent("""\
    id=$("$d" ps -aq --filter "label=com.docker.compose.project=$p" --filter label=com.docker.compose.service=prepare --filter label=com.docker.compose.oneoff=False)
    case "$id" in ''|*[!0-9a-f]*) echo "找不到本站唯一的 prepare 容器，请核对项目名。" >&2; exit 1;; esac""")


def host_path(value):
    """Unknown or unusual paths are discovered from the installed Compose container."""
    if not value or not value.startswith("/") or any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return PurePosixPath(value)


def station_path(project_dir, value):
    if value.startswith("/"):
        return host_path(value)
    return host_path(str(project_dir / value)) if project_dir else None


def host_setup(project, project_dir, station, config):
    setup = f'd={DSM_DOCKER}\n[ -x "$d" ] || d=docker\np={quote(project)}'
    if project_dir and station and config:
        return setup + f"\ndir={quote(str(project_dir))}\nstation={quote(str(station))}\ncfg={quote(str(config))}"
    # Mountinfo exposes kernel filesystem roots (e.g. btrfs subvolumes), not the
    # Compose working directory. Read the real labels on the *host*, without adding
    # a Docker socket to the installer or exposing it to the site's containers.
    return setup + "\n" + FIND_PREPARE + "\n" + dedent("""\
        dir=$("$d" inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$id")
        cfg=$("$d" inspect -f '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$id")
        station=$("$d" inspect -f '{{range .Mounts}}{{if eq .Destination "/station"}}{{.Source}}{{end}}{{end}}' "$id")
        case "$dir" in /*) ;; *) echo "缺少项目的绝对目录。" >&2; exit 1;; esac
        case "$cfg" in /*) ;; *) echo "缺少项目的 YAML 路径。" >&2; exit 1;; esac
        case "$station" in /*) ;; *) echo "缺少站点目录。" >&2; exit 1;; esac
        [ -d "$dir" ] && [ -f "$cfg" ] && [ -d "$station/data" ] || { echo "请核对项目目录、单份 YAML 和站点目录。" >&2; exit 1; }""")


def render(project, url, identity, *, project_dir=None, site_dir=".", config_file=None):
    directory = host_path(project_dir)
    station = station_path(directory, site_dir)
    config = host_path(config_file)
    instance = identity["instance_id"]
    root_sha = identity["tls"].get("root_sha256")
    setup = host_setup(project, directory, station, config)
    # Compose may originally have received its paths, IP and port from shell
    # variables. A DSM scheduled task gets a fresh environment every time.
    address = urlsplit(url)
    compose = ('GIFTLEDGER_PROJECT_DIR="$dir" GIFTLEDGER_SITE_DIR="$station" '
               f'GIFTLEDGER_SITE_IP={address.hostname} GIFTLEDGER_HTTPS_PORT={address.port or 443} '
               '"$d" compose --project-directory "$dir" -f "$cfg" -p "$p"')

    def block(command, *, discover=False):
        preamble = host_setup(project, None, None, None) if discover else setup
        return "(\nset -eu\n" + preamble + "\n" + command + "\n)"

    fingerprint = f"根证书 SHA-256    {root_sha}" if root_sha else "此站沿用早期版本的公开证书。"
    paragraphs = [f"""往来有礼 · 本站维护说明

网站    {url}/
项目名    {project}
实例    {instance}
项目文件夹    {directory or '在服务器运行下面的“查看本站目录”，即可读出绝对路径。'}
站点文件夹    {station or '随“查看本站目录”一同显示。'}
项目 YAML    {config or '随“查看本站目录”一同显示。'}
{fingerprint}

服务器维护命令在群晖的 root 任务计划、root SSH 终端或 Ubuntu 上有 Docker 权限的终端执行。Mac 和 Windows 的登记、恢复命令请在对应的站主工具目录执行。多行命令请连同括号一起复制。"""]
    resolved = all((directory, station, config))
    get_dirs = 'printf "项目文件夹：%s\\nYAML：%s\\n站点文件夹：%s\\n" "$dir" "$cfg" "$station"'
    update = (compose + ' run --rm --no-deps -e GIFTLEDGER_PROJECT_DIR="$dir" '
              '-e GIFTLEDGER_SITE_DIR="$station" -e GIFTLEDGER_GUIDE_CONFIG_FILE="$cfg" prepare refresh-guide')
    if not resolved:
        paragraphs.append("查看本站目录\n请先在宿主机运行这段，读回项目实际使用的路径，并将其填入本说明。"
                          "完成后重新打开文件，Windows 和 Mac 的登记、恢复命令会一同补齐。\n" + block(get_dirs + "\n" + update))
    credentials = ("第一次密码用于 SSH 登录。若随后出现管理员授权提示，请先看菜单栏输入法；"
                   "英文密码选 ABC，输入同一服务器账号的密码，再按回车。终端输入密码时没有回显。")

    def mac_command(program):
        return ("(\nset -eu\n" + 'printf "服务器的 SSH 用户名："; IFS= read -r gl_user\n'
                + 'printf "SSH 端口（默认 22，直接回车）："; IFS= read -r gl_port\n'
                + f'python3 {program} --host "${{gl_user}}@{address.hostname}" --port "${{gl_port:-22}}" '
                + f'--project-dir {quote(str(directory))} --project {quote(project)} '
                + f'--compose-file {quote(str(config))} --site-dir {quote(str(station))} --site-url {quote(url)}\n)')

    def windows_command(action):
        paths = [str(directory), str(station), str(config)]
        if all(re.fullmatch(r"[\w ./-]+", path) for path in paths):
            layout = f'--project-dir "{directory}" --site-dir "{station}" --compose-file "{config}"'
        else:
            # PowerShell and cmd.exe both pass this token to the batch launcher
            # literally, even for Unix paths containing quotes or %variables%.
            encoded = base64.urlsafe_b64encode(json.dumps(paths, ensure_ascii=False).encode("utf-8")).decode("ascii")
            layout = "--site-layout " + encoded
        return f'.\\GiftLedger.cmd {action} --project {project} --site-url {url} {layout}'

    enrollment = ("第一次备份前登记钥匙\n从零建账的新站和用旧备份恢复的新站都可以选择“登记旧钥匙”或“生成新钥匙并登记”。\n"
                  "从同一发行版本下载站主工具。Windows 解压后双击 GiftLedger.cmd，选择登记钥匙。"
                  f"SSH 主机地址是 {address.hostname}，项目名是 {project}，项目文件夹见上方。")
    if resolved:
        enrollment += ("\nWindows 请在解压好的站主工具目录打开 PowerShell，复制下面这一行。按提示输入服务器的 SSH 账号和端口。\n"
                       + windows_command("enroll"))
        enrollment += ("\nMac 请在同版本的 Mac 站主工具目录运行下面这段命令，然后在工具里选择。\n"
                       "选“登记旧钥匙”时，请选取自己保管的 .agekey 原文件。\n"
                       "选“生成新钥匙并登记”时，请确认新文件的保存位置，保管好工具生成的 .agekey 原文件。\n"
                       + credentials + "\n" + mac_command("mac_backup_key.py") +
                       "\n如果钥匙文件已生成但登记中断，再次运行上面同一段命令，"
                       "选择“登记旧钥匙”，选取刚才生成的原文件即可继续登记。")
    else:
        enrollment += "\n先按上段“查看本站目录”操作，再打开本文件复制登记命令。"
    enrollment += "\n已经看到“本站备份钥匙登记通过”后，就可以运行手动备份。"
    paragraphs.append(enrollment)
    # Copy the DSM helper from the installed, read-only prepare image. A root
    # scheduled task must not execute a script replaceable on the user's share.
    copy_program = ('tmp=$(mktemp -d)\n'
                    'trap \'rm -f "$tmp/backup.pyz"; rmdir "$tmp"\' EXIT\n'
                    '"$d" cp "$id:/opt/gift-ledger-install/dsm-backup.pyz" "$tmp/backup.pyz"')
    if resolved:
        copy_program = FIND_PREPARE + "\n" + copy_program
    run_program = f'{quote(DSM_PYTHON)} -I "$tmp/backup.pyz"'
    site_args = '--project "$p" --station "$station"'
    dsm_backup = copy_program + "\n" + run_program + " " + site_args
    paragraphs.append("手动备份\n首次使用须先用同版站主工具登记本站备份钥匙。群晖需要服务器上的 Python 3.8 或以上版本。任务会先核对 Container Manager 的版本文件，目前支持 24.0.2-1543 和 24.0.2-1606。开头若显示 CM_vendor_file_set_unreviewed，任务会在停止网站之前退出；请将套件中心显示的版本号和任务运行结果反馈给项目维护者。"
                      "在“控制面板 → 任务计划”建立停用排程的用户自定义脚本，"
                      "用户选 root，粘贴本段，保存后点“运行”。Ubuntu 在服务器终端粘贴本段。备份期间网站会短暂暂停。\n" +
                      block('cd "$dir"\nif [ -x ' + DSM_DOCKER + ' ]; then\n  ' +
                            dsm_backup.replace('\n', '\n  ') + '\nelse\n  ' + compose + ' run --rm backup\nfi') +
                      "\n看到“已实际解密核验”后，将站点文件夹中 backups/artifacts/ 下本次编号对应的整份五文件备份复制到另一台设备。原钥匙另行保管。")
    if root_sha:
        paragraphs.append("核对公开根证书\n从本站 data 文件夹下载“本站根证书.cer”。若 DSM 日志页为空，以下命令会从 Docker 日志读取指纹，再计算服务器上该证书的 SHA-256。"
                          "Mac 可在终端输入 shasum -a 256 和一个空格，将刚下载的文件拖入终端再按回车；"
                          "这样同名下载带有“(1)”时也能选对文件。指纹一致后再信任证书。\n" +
                          block(compose + ' logs --no-color prepare | grep -F "本站根证书 SHA-256" || :\n'
                                'sha256sum "$station/data/本站根证书.cer"'))
        paragraphs.append("查看证书有效期\n本段检查本站证书并显示到期信息。\n" +
                          block(compose + " run --rm --no-deps certificate inspect"))
        certificate_check = (run_program + " --check-certificate " + site_args +
                             f" --url {quote(url + '/')} --root-sha256 {quote(root_sha)}")
        scheduled_renew = (copy_program + "\nif " + certificate_check +
                           '; then\n  exit 0\nelse\n  rc=$?\n  [ "$rc" -eq 3 ] || exit "$rc"\nfi\n' +
                           compose + " run --rm --no-deps -T certificate auto-renew")
        paragraphs.append("群晖自动续签网站证书\n在 DSM 为本站设置一次，每天检查。网站证书剩余 30 天或更短时才续签，沿用已信任的根证书。"
                          "实际换证时本站网页会短暂停用，请选平时少有人记账的时间，并与备份任务错开。\n"
                          "1. 打开“控制面板 → 任务计划 → 新增 → 计划的任务 → 用户定义的脚本”。\n"
                          f"2. 在“常规”中将任务命名为 GiftLedger TLS {address.port or 443}，用户选 root，勾选“已启用”。"
                          "在“计划”中选每天一次，例如凌晨 03:15。\n"
                          "3. 在“任务设置”的脚本栏粘贴下面整段命令，连同首尾括号一起复制。"
                          "若已配置 DSM 邮件通知，可填写收件邮箱并勾选仅在脚本异常终止时发送运行详情。\n" +
                          block(scheduled_renew) +
                          "\n4. 保存后选中任务，点“运行”，再打开“查看结果”。若网站证书还有 30 天以上，"
                          "任务应显示“正常 (0)”，到期时间保持原值。若任务报告根证书有效期不足，按“根证书临近到期”安排迁站。")
        paragraphs.append("续签本站 HTTPS 证书\n需要立即换证时，确认上方项目、网址和实例，在方便短暂停用本站网页时运行下段。"
                          "新签发的网站证书有效期为 365 天。完成后打开原网址，再运行“查看证书有效期”核对新的到期时间。\n" +
                          block(compose + " run --rm --no-deps certificate renew"))
        paragraphs.append("根证书临近到期\n本版新建站点的根证书到公元 9999 年 12 月 31 日 23:59:59 UTC。"
                          "已有站点继续使用原根证书和它原有的到期日。自动续签只处理网站证书。"
                          "若本站根证书的剩余有效期不足以签发完整的 365 天网站证书，续签任务会报告失败。"
                          "请在当前网站证书到期前备份并恢复到独立新站，核对账本和新站根证书指纹，在使用本站的设备上信任新根证书后切换网址。")
    else:
        paragraphs.append("证书维护\n本站公开文件是 data/本站公开证书.crt。此站沿用早期自签证书布局，"
                          "请按原版本的证书维护说明操作。")
    paragraphs.append("从本站备份恢复到另一站\n在独立的项目文件夹用独立项目名和其他 HTTPS 端口启动一个新站，保留空站状态。"
                      "打开新站 data/本站维护说明.txt，在它的“恢复到本站”一节操作。")
    recovery = (f"恢复到本站（目标 {project}，{url}/）\n仅在这里就是新建的空目标站时执行。"
                "在 Windows 或 Mac 打开同版站主工具目录，准备源站的五文件备份或 ZIP 与匹配的原钥匙。")
    if resolved:
        recovery += ("\nWindows 请在 PowerShell 运行下面这一行。目标站的 HTTPS 网址和端口已填好，工具会先核对该项目的 Web 端口映射。\n"
                     + windows_command("restore") +
                     "\nMac 请运行下面这段，按提示拖入或选择备份、原钥匙。\n" + credentials + "\n" + mac_command("mac_restore.py"))
    else:
        recovery += "先按“查看本站目录”操作，再打开本文件复制恢复命令。"
    paragraphs.append(recovery + f"\n确认框应显示备份来源、目标网址 {url}/ 和本站实例 {instance}。"
                      "核对一致后确认。恢复完成，用原账号登录目标站，核对最后一笔账和收支合计。\n"
                      "把用旧备份恢复的新站作为主力站后，首次备份前请看本文件的“第一次备份前登记钥匙”。"
                      "恢复时临时提供旧钥匙用于解密。如果本站还没登记日常备份钥匙，可选“登记旧钥匙”沿用这把钥匙，也可选“生成新钥匙并登记”。"
                      "登记过一次，以后直接运行本文件的“手动备份”。每份备份都要保留与它匹配的 .agekey 原文件。")
    if resolved:
        paragraphs.append("重新核对项目路径\n如果项目移动过或 YAML 改过名字，先在宿主机运行本段重新读取当前路径，再打开本文件。\n"
                          + block(get_dirs + "\n" + update, discover=True))
    return "\n\n".join(paragraphs) + "\n"

