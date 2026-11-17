# 往来有礼

在家中局域网或 VPN 上为自己和家人部署礼金账本。首版支持 Linux/amd64 的 Ubuntu 与群晖 Container Manager。[English](README.en.md)

## 安装

从同一发行版本下载一个 [docker-compose.yml](https://github.com/bujishx/gift-ledger/releases/latest/download/docker-compose.yml)。在专用空目录中保存它，用文本编辑器将顶部的局域网 IP 和端口改为本站的地址，例如 `192.168.1.20` 和 `8443`。确保端口空闲，首次安装时服务器可访问 Docker Hub。

- **Ubuntu** 已安装 Docker Engine 与 Compose 插件后，在 YAML 目录执行 `docker compose up -d`。
- **群晖** 将 YAML 放在例如 `/volume1/docker/gift-ledger` 的文件夹。打开 Container Manager → 项目 → 新增，项目名填 `gift-ledger`，选该文件夹并使用已有 YAML。

在 `data/网站地址.txt` 查看实际网址。把 `data/本站根证书.cer` 送到手机，与本站 `prepare` 日志的 SHA-256 核对后按系统方法信任证书。手机连家中 Wi-Fi 或 VPN，打开网址，注册、完成本人建档、记一笔。重启服务器后用原账号登录，确认这笔账仍在。忘记账号或密码，请联系本站站主。

## 备份

在 Mac 下载同版本的 [站主工具](https://github.com/bujishx/gift-ledger/releases/latest/download/mac-site-tools.zip)，按包内《从备份恢复》首次登记备份钥匙。随后 Ubuntu 在 YAML 目录执行 `docker compose run --rm backup`。群晖在“控制面板 → 任务计划”创建一项停用排程的用户自定义脚本，用户选 root，保存后选中它并点“运行”。脚本使用本站的实际项目名和文件夹。

```sh
cd '/volume1/docker/gift-ledger' && /var/packages/ContainerManager/target/usr/bin/docker compose -p gift-ledger run --rm backup
```

将提示的整个五文件备份目录复制到另一台设备，私钥另行保管。换机按同版本工具包内的说明恢复到独立空站，核对账本后切换。已经安装的 YAML 固定了镜像摘要，站主自行选择升级时间。

站主可查阅[账号找回](docs/站主找回账号.md)和[升级与回退](docs/站主升级.md)。
