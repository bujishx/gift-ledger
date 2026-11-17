# GiftLedger

A self-hosted gift ledger for your LAN or VPN. The initial release supports Linux/amd64 on Ubuntu and Synology Container Manager. [简体中文](README.md)

## Install

Download the [docker-compose.yml](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/docker-compose.yml) into a dedicated empty folder. Set the LAN IP and an available HTTPS port at the top of the file, for example `192.168.1.20` and `8443`. Your server needs access to Docker Hub on first installation.

- **Ubuntu** With Docker Engine and the Compose plugin installed, run `docker compose up -d` in that folder.
- **Synology** Put the YAML in a folder such as `/volume1/docker/gift-ledger`. In Container Manager, open **Project → Create**, use the project name `gift-ledger`, select the folder and use its existing YAML.

Open the URL written to `data/网站地址.txt`. Transfer `data/本站根证书.cer` to your phone, compare its SHA-256 fingerprint with the `prepare` service log and trust the certificate on the phone. Connect to your LAN or VPN, register, finish your profile and record one gift. Restart the server and confirm that the same account still shows that gift. Contact the site owner if you forget your login name or password.

## Back up

Download the matching [Mac tools](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/mac-site-tools.zip) and unpack them. On a Mac with Python 3 and age installed, register a backup key once, substituting your own SSH account and project path.

```sh
python3 mac_backup_key.py --host siteadmin@192.168.1.20 --project-dir /volume1/docker/gift-ledger --project gift-ledger --generate "$HOME/GiftLedger.agekey"
```

On Ubuntu run `docker compose run --rm backup` in the YAML directory. On Synology create a user-defined script in **Control Panel → Task Scheduler**, choose root, turn off scheduled execution, save and select **Run** when needed. Adjust the project name and folder in this command.

```sh
cd '/volume1/docker/gift-ledger' && /var/packages/ContainerManager/target/usr/bin/docker compose -p gift-ledger run --rm backup
```

Copy the entire five-file backup folder printed on success to another device. Keep your private key separately. To restore, install a fresh empty site and run `python3 mac_restore.py --host siteadmin@192.168.1.20 --project-dir /volume1/docker/gift-ledger --project gift-ledger` from the same Mac tool folder. Select the backup and original key, verify the destination and confirm. Review the recovered ledger before switching sites. Each release's YAML pins exact image digests, so you choose when to upgrade.

Site owner references in Chinese cover [account recovery](docs/站主找回账号.md) and [upgrades and rollback](docs/站主升级.md).
