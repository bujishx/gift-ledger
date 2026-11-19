"""Render the public UTF-8 owner's notes. Data paths in this file are host paths."""

import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
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


def shipped_digest(filename):
    path = Path(__file__).with_name(filename)
    return hashlib.sha256(__loader__.get_data(str(path))).hexdigest()


def host_helper_command(project, url, instance, filename, action, *, interactive=False):
    find = FIND_PREPARE.replace('"$d" ps', '"$d" --host unix:///var/run/docker.sock ps')
    guard = '[ "$(id -u)" -eq 0 ]'
    if interactive:
        guard += ' && [ -t 0 ] && [ -t 1 ] && [ -t 2 ]'
    return (
        "(\nset -eu\n" + guard
        + ' || { echo "请在服务器管理员终端运行（sudo -i）。" >&2; exit 1; }\n'
        + f'd={DSM_DOCKER}\n[ -x "$d" ] || d=/usr/bin/docker\n'
        + '[ -x "$d" ] || d=/usr/local/bin/docker\n'
        + f'p={quote(project)}\n' + find + '\n'
        + 'station=$("$d" --host unix:///var/run/docker.sock inspect -f '
          "'{{range .Mounts}}{{if eq .Destination \"/station\"}}{{.Source}}{{end}}{{end}}' "
          '"$id")\n'
        + 'tmp=$(mktemp -d /tmp/giftledger-owner.XXXXXXXX)\n'
        + f"trap 'rm -f \"$tmp/{filename}\"; rmdir \"$tmp\"' EXIT\n"
        + f'"$d" --host unix:///var/run/docker.sock cp "$id:/opt/gift-ledger-install/{filename}" "$tmp/{filename}"\n'
        + f'printf "%s  %s\\n" {shipped_digest(filename)} "$tmp/{filename}" | sha256sum -c -\n'
        + f'/usr/bin/python3 -I "$tmp/{filename}" {action} --project "$p" --station "$station" '
        + f'--instance {quote(instance)} --origin {quote(url)}\n)'
    )


def login_code_command(project, url, instance):
    return host_helper_command(project, url, instance, "owner_login_code.py", "issue", interactive=True)


def render(project, url, identity, *, project_dir=None, site_dir=".", config_file=None, owner_login=False):
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
    if owner_login:
        installed = quote(f"/root/.giftledger-maintenance/{project}/maintenance-install.pyz")
        paragraphs.append("启用本站网页维护（每站一次）\n在服务器终端执行 sudo -i，复制下面整段。它从本站 prepare 容器取出同版程序，"
                          "请先确认项目使用同一发行包的 YAML 和其中固定的镜像。命令核对 SHA-256、网址、项目及实例后安装，只为本站处理维护任务。Ubuntu 会启用本站的开机服务和证书定时任务。\n"
                          + host_helper_command(project, url, instance, "maintenance-install.pyz", "install")
                          + "\n群晖还需在“控制面板 → 任务计划 → 新增 → 触发的任务 → 用户定义的脚本”新建并启用任务，"
                          f"用户选 root，事件选“开机”，名称可填 GiftLedger {project}。将下面一行粘贴到“任务设置”并保存。\n"
                          f"/usr/bin/python3 -I {installed} start --project {quote(project)}")
        paragraphs.append(
            f"站点维护网页    {url.rstrip('/')}/maintenance\n"
            "从下方领取本站登录码，或用已设置的站主密码登录。登录后核对页面显示的项目名。\n"
            "页面出现“首次备份前登记钥匙”时，从零建账的站和用旧备份恢复的新站都可以选择“登记旧钥匙”或"
            "“生成新钥匙并登记”。新钥匙先下载保存，再选中保存的文件，点“确认登记到本站”。\n"
            "看到“本站备份钥匙登记通过”后，点“开始备份”。等网站重新打开，点“查看备份结果”，"
            "再点“下载已核验的备份 ZIP”。请将 ZIP 和对应的原钥匙分别妥善保管。\n"
            "想把旧备份恢复到独立空站时，打开目标空站的维护网页。选择旧站的备份和制作这份备份时用的原钥匙，"
            "点“上传并预检”。核对显示的源站、笔数、目标网址和项目后，输入“恢复”，点“确认恢复到本站”。"
            "稍后在同一页点“查看恢复进度”，再用原账号登录核账。"
        )
        paragraphs.append("站主维护登录码（群晖和 Ubuntu）\n在本站服务器的交互终端用 sudo -i 进入管理员身份，"
                          "复制下方整段。核对终端显示的项目和网址，在本站维护页选择一次性码登录；可直接登录，无需登录名。"
                          "每个码有效 10 分钟，用过即失效，重新领码会使上一个未用码失效。请在终端内查看，勿放入任务计划。\n"
                          + login_code_command(project, url, instance))
        paragraphs.append("网页暂时打不开时查看本站维护任务\n备份或恢复时可稍后重新打开原网址。长时间未恢复，可在服务器管理员终端复制下面一行查看任务状态。"
                          "先记下原任务编号，如状态为 needs_review，请根据程序提供的救援命令核对这次任务。\n"
                          f"/usr/bin/python3 -I {installed} status --project {quote(project)}")
    resolved = all((directory, station, config))
    get_dirs = 'printf "项目文件夹：%s\\nYAML：%s\\n站点文件夹：%s\\n" "$dir" "$cfg" "$station"'
    update = (compose + ' run --rm --no-deps -e GIFTLEDGER_PROJECT_DIR="$dir" '
              '-e GIFTLEDGER_SITE_DIR="$station" -e GIFTLEDGER_GUIDE_CONFIG_FILE="$cfg" prepare refresh-guide')
    credentials = ("第一次密码用于 SSH 登录。若随后出现管理员授权提示，请先看菜单栏输入法；"
                   "英文密码选 ABC，输入同一服务器账号的密码，再按回车。终端输入密码时没有回显。")

    def mac_command(program):
        if resolved:
            target = (f'--project-dir {quote(str(directory))} --project {quote(project)} '
                      f'--compose-file {quote(str(config))} --site-dir {quote(str(station))}')
        else:
            target = f'--project {quote(project)} --instance-id {quote(instance)}'
        return ("(\nset -eu\n" + 'printf "服务器的 SSH 用户名："; IFS= read -r gl_user\n'
                + 'printf "SSH 端口（默认 22，直接回车）："; IFS= read -r gl_port\n'
                + f'python3 {program} --host "${{gl_user}}@{address.hostname}" --port "${{gl_port:-22}}" '
                + f'{target} --site-url {quote(url)}\n)')

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

    enrollment_heading = "备用方式 · 用桌面工具登记钥匙" if owner_login else "第一次备份前登记钥匙"
    enrollment = (f"{enrollment_heading}\n从零建账的新站和用旧备份恢复的新站都可以选择“登记旧钥匙”或“生成新钥匙并登记”。\n"
                  f"本次登记的项目是 {project}，网址是 {url}/。请按电脑系统下载与本站同一发行版本的站主工具。\n"
                  "Mac 和 Windows 都会优先使用电脑上与本站匹配的 age 和 age-keygen。首次提示缺少时，按回车下载，"
                  "核对后会继续本次操作。在这台电脑上可复用相同版本的加密工具。如要使用本机 Docker Desktop 备用，按提示输入 D。")
    enrollment += "\n\nWindows\n解压 windows-site-tools.zip。可以双击 GiftLedger.cmd，选择登记钥匙。"
    if resolved:
        enrollment += ("要使用已填好本站资料的命令，在解压后的工具文件夹打开 PowerShell，"
                       "复制下面这一行运行，按提示输入服务器的 SSH 账号和端口。\n" + windows_command("enroll"))
    else:
        enrollment += "如需已填好项目路径的 PowerShell 命令，可按本文件的“查看本站目录”更新说明。"

    enrollment += ("\n\nMac\n解压 mac-site-tools.zip。在 Mac 上打开“终端”，输入 cd 和一个空格，"
                   "把含有 mac_backup_key.py 的工具文件夹拖进终端，按回车。")
    enrollment += "把下面从 ( 到 ) 的整段登记命令复制到这个 Mac 终端运行，按提示输入服务器的 SSH 账号和端口。"
    if not resolved:
        enrollment += "工具登录后会自动查找本站目录，并在登记前核对站点身份。"
    enrollment += "\n" + credentials + "\n" + mac_command("mac_backup_key.py")

    enrollment += ("\n\n在登记工具中选择\n“登记旧钥匙”会让你选取自己保管的 age 私钥原文件，例如 identity.txt 或以 .agekey 结尾的文件。\n"
                   "“生成新钥匙并登记”会让你选择新钥匙的保存位置，请保管好生成的 .agekey 原文件。\n"
                   "如果新钥匙已生成但登记中断，再次打开登记工具，选择“登记旧钥匙”，选取刚才生成的文件即可继续。\n"
                   "看到“本站备份钥匙登记通过”后，就可以运行手动备份。")
    paragraphs.append(enrollment)
    if not resolved:
        paragraphs.append("查看本站目录\n需要查看服务器上的实际目录或生成含完整路径的恢复命令时，在服务器终端运行下面整段命令。"
                          "它会更新站点文件夹里的 data/本站维护说明.txt。完成后打开更新后的文件。\n" + block(get_dirs + "\n" + update))
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
    paragraphs.append("手动备份\n首次使用须先登记本站备份钥匙。群晖需要服务器上的 Python 3.8 或以上版本。任务会先核对 Container Manager 的版本文件，目前支持 24.0.2-1543 和 24.0.2-1606。开头若显示 CM_vendor_file_set_unreviewed，任务会在停止网站之前退出；请将套件中心显示的版本号和任务运行结果反馈给项目维护者。"
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
        unit = f"giftledger-certificate-{project}"
        paragraphs.append("Ubuntu 自动续签网站证书\n在 Ubuntu 上安装本站的宿主维护服务和证书定时任务后，"
                          "复制下面三行即可启用并查看本站排程。默认按服务器当地时间每天 03:15 检查，"
                          "错过排程会在下次开机后补查。网站证书剩余 30 天或更短时才续签，沿用本站根证书。\n"
                          f"sudo systemctl enable --now {unit}.timer\n"
                          f"sudo systemctl start {unit}.service\n"
                          f"systemctl list-timers --all {unit}.timer\n"
                          "运行结果可用下面一行查看。请留意失败原因，尤其是根证书临近到期的提示。\n"
                          f"sudo journalctl -u {unit}.service -n 20 --no-pager")
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
                      f"把用旧备份恢复的新站作为主力站后，首次备份前请看本文件的“{'站点维护网页' if owner_login else enrollment_heading}”。"
                      "恢复时临时提供旧钥匙用于解密。如果本站还没登记日常备份钥匙，可选“登记旧钥匙”沿用这把钥匙，也可选“生成新钥匙并登记”。"
                      "登记过一次，以后直接运行本文件的“手动备份”。每份备份都要保留与它匹配的 .agekey 原文件。")
    if resolved:
        paragraphs.append("重新核对项目路径\n如果项目移动过或 YAML 改过名字，先在宿主机运行本段重新读取当前路径，再打开本文件。\n"
                          + block(get_dirs + "\n" + update, discover=True))
    return "\n\n".join(paragraphs) + "\n"

