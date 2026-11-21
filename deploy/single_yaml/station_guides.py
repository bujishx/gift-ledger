"""Offline material for owners. The pre-install text is also a release attachment."""
from pathlib import Path
from shlex import quote
from urllib.parse import urlsplit

import owner_guide

NETWORK_GUIDE = "安装前检查端口与网络.txt"
RETIRE_GUIDE = "如何彻底删除本站.txt"


def documents(project, url, instance, *, project_dir=None, site_dir=None, config_file=None):
    """Return only public guide bytes; server account names and secrets are never requested."""
    # The DSM host archive uses zipimport, where these resources are not real paths.
    network = __loader__.get_data(str(Path(__file__).with_name(NETWORK_GUIDE)))
    return {
        NETWORK_GUIDE: network,
        owner_guide.public_name(project, instance, RETIRE_GUIDE): retirement(project, url.rstrip("/"), instance, project_dir, site_dir, config_file).encode(),
    }


def retirement(project, origin, instance, project_dir, site_dir, config_file):
    # Discover current paths at use time even when the saved guide was copied elsewhere.
    directory = owner_guide.host_path(str(project_dir or ""))
    station = owner_guide.host_path(str(site_dir or ""))
    config = owner_guide.host_path(str(config_file or ""))
    local_docker = "unset DOCKER_CONTEXT\nexport DOCKER_HOST=unix:///var/run/docker.sock"
    discover = local_docker + "\n" + owner_guide.host_setup(project, None, None, None)
    address = urlsplit(origin)
    qproject = quote(project)
    qinstance = quote(instance)
    host = f"/root/.giftledger-maintenance/{project}"
    service = f"giftledger-maintenance-{project}.service"
    cert = f"giftledger-certificate-{project}"
    alerts = f"giftledger-backup-alert-{project}"
    snapshot = f"""(
set -eu
{discover}
test "$(id -u)" -eq 0
/usr/bin/python3 -I -c 'import json,sys; from pathlib import Path; site=json.loads((Path(sys.argv[1])/"data/root_config/site/control/identity.json").read_bytes()); sys.exit(0 if (site.get("project"),site.get("instance_id"))==tuple(sys.argv[2:]) else "项目或实例不符，停止")' "$station" "$p" {qinstance}
printf '项目 %s\\n项目目录 %s\\n站点目录 %s\\nYAML %s\\n' "$p" "$dir" "$station" "$cfg"
"$d" ps -a --filter "label=com.docker.compose.project=$p" --format 'table {{{{.ID}}}}\\t{{{{.Names}}}}\\t{{{{.Status}}}}'
"$d" network ls --filter "label=com.docker.compose.project=$p"
"$d" volume ls --filter "label=com.docker.compose.project=$p"
ids=$("$d" ps -aq)
for c in $ids; do
  "$d" inspect --format '{{{{.Name}}}}  {{{{range .Mounts}}}}{{{{.Type}}}}:{{{{.Source}}}} -> {{{{.Destination}}}}  {{{{end}}}}' "$c"
done
)"""
    # Include the immutable instance in the down block. A guide saved before a rebuild
    # must not act on another site that later reuses the same path, name or port.
    confirm_down = f"""(
set -eu
{discover}
/usr/bin/python3 -I -c 'import json,sys; from pathlib import Path; v=json.loads((Path(sys.argv[1])/"data/root_config/site/control/identity.json").read_bytes()); sys.exit(0 if (v.get("project"),v.get("instance_id"))==tuple(sys.argv[2:]) else "实例已变化，停止")' "$station" "$p" {qinstance}
printf '这会删除 %s 的容器、网络和卷。确认已核对清单，请输入完整项目名：' "$p"
IFS= read -r answer
[ "$answer" = "$p" ] || exit 1
GIFTLEDGER_PROJECT_DIR="$dir" GIFTLEDGER_SITE_DIR="$station" GIFTLEDGER_SITE_IP={quote(address.hostname)} GIFTLEDGER_HTTPS_PORT={address.port or 443} "$d" compose --project-directory "$dir" -f "$cfg" -p "$p" --profile maintenance down --volumes
)"""
    verify = f"""(
set -eu
{local_docker}
d={owner_guide.DSM_DOCKER}
[ -x "$d" ] || d=/usr/bin/docker
p={qproject}
"$d" ps -a --filter "label=com.docker.compose.project=$p" --format '{{{{.Names}}}}'
"$d" network ls --filter "label=com.docker.compose.project=$p" --format '{{{{.Name}}}}'
"$d" volume ls --filter "label=com.docker.compose.project=$p" --format '{{{{.Name}}}}'
)"""
    # DSM ps implementations vary and may truncate long command lines. Display
    # only the single root-owned worker whose full argv matches this installation.
    dsm_worker = f"""(
set -eu
/usr/bin/python3 -I - {quote(host)} {qinstance} <<'PYWORKER'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
record = json.loads((root / "installation.json").read_bytes())
if record["instance"] != sys.argv[2] or record["project"] != root.name:
    sys.exit("宿主维护目录的实例不符")
args = ["/usr/bin/python3", "-I", str(root / "maintenance_host.py"), "serve", "--state",
        str(root / "state"), "--persistent", "--key-enrollment", "--web-restore", "--key-image", record["image"]]
for pid in Path("/proc").iterdir():
    if pid.name.isdecimal():
        try:
            if pid.stat().st_uid == 0 and (pid / "cmdline").read_bytes().split(bytes([0])) == [v.encode() for v in args] + [b""]:
                print("本站维护 PID", pid.name, root)
        except (FileNotFoundError, ProcessLookupError):
            continue
PYWORKER
)"""
    return f"""往来有礼 · 如何彻底删除本站

项目　{project}
网址　{origin}/
实例　{instance}
项目目录　{directory or '请在服务器执行第 1 步，读取群晖或 Ubuntu 的实际目录。'}
站点目录　{station or '见第 1 步的“站点目录”。'}
YAML　{config or '见第 1 步的“YAML”。'}

把本文件先保存到站点之外，退站后仍可用它收尾。只想暂时停止网站时，在 Container Manager 停止本站项目或在 Ubuntu 停止本站 Compose 项目即可。以下步骤用于永久移除。

1　列出本站资源，决定是否留账

请在本站维护页确认当前没有进行中的备份、恢复、钥匙或续签操作。准备留账时先停止记账，完成一次备份并实际解密核验。把完整备份移到另一台设备，单独保存制作这份备份时的原私钥。若已决定放弃本网站所有账本和站内备份，可直接清点。

在群晖 SSH 或 Ubuntu 服务器执行 sudo -i。打开本机 Docker 的管理员终端，复制下段（只读）。它从已创建的 prepare 容器取回真实目录并比对实例。请保存输出，记录其他站当前是运行还是停止。若找不到唯一的 prepare，或项目、实例、网址与待删目标不一致，先查清原站身份。

{snapshot}

最后一张表会列出所有容器的挂载。检查本站的项目目录、数据目录以及专属卷是否被别的项目使用，也核对以后还会启动的其他项目 YAML。遇到共享路径、另挂的存储或自行指定的备份目的地，分别判断其归属。共享文件夹的父目录应保留。若网页维护已安装，还可用这条只读命令核对项目、实例和作业状态。

/usr/bin/python3 -I {host}/maintenance-install.pyz status --project {qproject}

2　停止本站排程与维护进程（选择自己的平台）

请在已约好的退站时间操作。最后一次备份完成后，暂停本站网页入口（仅停止本项目的 web 容器），再核对本站没有未完成或等待收尾的任务。这样不会在退站中途接到新的网页请求。

群晖
• 控制面板 → 任务计划。打开手动备份、备份提醒、HTTPS 续签、网页维护开机等任务的任务设置，逐条核对脚本内写的项目和实际目录。先停用并确认没有任务在运行，再删除属于本站的那几条任务。管理员改过的任务名称也要检查。
• 已启用网页维护时，开机任务停止后，原有维护进程可能仍在运行。在 root 终端输入下面的只读命令，它会按完整启动参数和安装记录查找本站进程。若有唯一匹配，确认状态空闲后执行 kill -TERM 实际PID，替换“实际PID”为刚核对的数字。再运行同一条查询，直到不再显示。若服务器已重启且本站开机任务已停用，则通常没有这条进程。

{dsm_worker}

Ubuntu
• 检查自己为本站建立的 cron 或额外定时任务，按脚本中的项目和目录辨认、停用后删除。在启用过新版网页维护的站，用下列命令查看本站生成的三个 systemd 定义。核对其中的项目、路径及实例绑定。未安装网页维护的站可以直接进入第 3 步。

systemctl cat {service} {cert}.timer {cert}.service {alerts}.timer {alerts}.service

• 确认续签服务没有在执行、维护页没有进行中的或待收尾操作后，停止并禁用本站的定时器和维护服务，移除已核实的三个专属单元定义。以下命令以 root 执行。系统管理员自行添加的覆盖文件请用 systemctl cat 显示的路径逐一审阅。

systemctl disable --now {cert}.timer {alerts}.timer {service}
systemctl stop {cert}.service {alerts}.service
rm -i -- /etc/systemd/system/{service} /etc/systemd/system/{cert}.timer /etc/systemd/system/{cert}.service /etc/systemd/system/{alerts}.timer /etc/systemd/system/{alerts}.service
systemctl daemon-reload

3　删除本站项目和目录

群晖：在 Container Manager → 项目 中选 {project}，先核对它在“项目”中显示的完整目录，再停止并删除该项目。回到第 4 步的资源检查。若有遗留专属卷或网络，先核对名称、站点标签和其他容器引用，再逐个删除。目录仍在时，在 File Station 进入第 1 步的实际位置，永久删除所选站点的专用文件夹，包括其中的 data、backups、隐藏的站点状态和恢复暂存目录。若删除后文件在共享文件夹回收站，只移除属于该站的那份。

Ubuntu：输入下段时确认当前 YAML 就是该项目真实生效的单份 YAML。命令再次比对站点实例，只处理这一个 Compose 项目。备份和账本所在的主机目录需要另外清理。

{confirm_down}

两个平台：已启用网页维护的站还要清理专属宿主目录 {host}。打开其中 installation.json，核对 project、instance、station 与本页和第 1 步相符，确认本站维护进程已退出。Ubuntu 可用 sudo rm -rI -- 加上已核实的单个绝对目录逐个确认删除。群晖的 root 终端可在核实后使用 rm -ri 加上这个宿主目录，逐项确认。项目目录与站点目录如相同只处理一次。按第 1 步记录核对 data 和 backups 的实际位置。启用外部备份、快照或共享目录时，只处理已确认属于本站的那部分。其他站的镜像、备份和设备上的证书或钥匙另行保管。

4　复查

下面三条清单应为空。分别按第 1 步保存的实际路径确认本站专用项目目录、站点目录、备份目录和宿主维护目录都已移除。在 DSM 确认项目和专属任务已消失。Ubuntu 确认 systemctl list-timers --all 中已无本站计时器。只打开原本就在运行的其他站，确认网址仍可访问。原本停止的站继续保持停止。

{verify}

若有经过核实的专属网络或卷遗留，先用 docker network inspect 或 docker volume inspect 加上完整名称核对标签和归属。网络需要没有其他成员，卷另用 docker ps -a --filter volume=完整卷名 核对没有容器引用。检查已停止和仅保留 YAML 的项目也不再需要它们。再用本机 Docker 的 network rm 或 volume rm 准确移除该单项，重复复查。
"""
