# GiftLedger

A self-hosted gift ledger for your LAN or VPN. The initial release supports Linux/amd64 on Ubuntu and Synology Container Manager. [简体中文](README.md)

Installation instructions cover both Ubuntu and Synology. The backup, recovery and certificate maintenance steps below have been tested on Synology DSM. Instructions for the updated Ubuntu workflows will follow testing on an Ubuntu host.

## Install

Download the [docker-compose.yml](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/docker-compose.yml) into a dedicated empty folder. Set the LAN IP and an available HTTPS port at the top of the file, for example `192.168.1.20` and `8443`. Your server needs access to Docker Hub on first installation.

- **Ubuntu** With Docker Engine and the Compose plugin installed, run `docker compose up -d` in that folder.
- **Synology** Put the YAML in a folder such as `/volume1/docker/gift-ledger`. In Container Manager, open **Project → Create**, use the project name `gift-ledger`, select the folder and use its existing YAML.

Open `data/网站地址.txt` for the URL and `data/本站维护说明.txt` for the site's project name, instance and ready-to-copy maintenance commands. If the host project folder is missing, run the complete “查看本站目录” block on the server and reopen the file.

Transfer `data/本站根证书.cer` to your phone or Mac and compare its SHA-256 fingerprint before trusting it. The “核对公开根证书” block reads the Docker log over SSH and checks the certificate file on the server, including when the DSM log viewer is empty. A second download may have `(1)` in the filename. Match the fingerprint. Connect to your LAN or VPN, open the site, register, finish your profile and record a gift.

On a Mac, import the root certificate whose fingerprint you verified into your login keychain. In Keychain Access, double-click this site's `GiftLedger Site CA` certificate in the Certificates list. Expand Trust and set Secure Sockets Layer (SSL) to Always Trust. Close the window, confirm the change if prompted, then open the site URL. [Apple’s Keychain Access guide](https://support.apple.com/guide/keychain-access/kyca11871/mac)

Restart the server and confirm the same account still shows that gift. Contact the site owner if you forget your login name or password. The installed YAML pins the image digests, so the site owner chooses when to upgrade.

## Back up

1. **Enroll a key before the site's first backup.** Whether you started a ledger from scratch or restored it from an old backup, follow “第一次备份前登记钥匙” in that site's notes. Open the enrollment menu in the matching [Windows tools](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/windows-site-tools.zip) or run the enrollment command in the [Mac tools](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/mac-site-tools.zip). The tool shows both options. Choose **登记旧钥匙** to enroll the original `.agekey` file you already keep, or **生成新钥匙并登记** to create and enroll a new key. If enrollment was interrupted after a key was created, run the same tool again, choose **登记旧钥匙**, and select that file. Keep the original key separately once enrollment succeeds.
2. **Run a manual backup on Synology.** Copy the complete “手动备份” command block from the site's notes. In DSM, create a user-defined script in **Control Panel → Task Scheduler**, select root, turn off the schedule, paste the block, save, then select **Run**.
3. **Verify and keep a copy.** When the tool confirms decryption, copy the entire five-file backup folder to another device and check the last entry and totals on your site.

**Restore the old site's backup to a new site from a Mac.** Start a separate empty site and leave it without any registered accounts. Open `data/本站维护说明.txt` on the **new site**. Copy the command under “恢复到本站” and run it in Terminal from the matching site tools directory on your Mac. The command includes the new site's address, project name and paths. When prompted, select the **old site's backup** and **the original key for that backup**. Check that the destination URL belongs to the new site before confirming. [Full recovery instructions](docs/从备份恢复.md)

**Keep using the site you restored from the old backup.** Before this site's first backup, choose whether to enroll the same key you used to restore the old backup or generate and enroll a new key. If this site already has a key enrolled, go straight to “手动备份” in its own notes. Keep the original `.agekey` file that matches each backup. [Back up the site restored from an old backup](docs/从备份恢复.md#用旧备份恢复的新站今后怎样备份)

The Synology manual backup supports Container Manager 24.0.2-1543 and 24.0.2-1606. It checks the package files at startup. If this check returns `CM_vendor_file_set_unreviewed`, the task exits before stopping the site. Report the Container Manager version shown in Package Center and the run result from Task Scheduler.

## Certificate expiry and renewal

New and renewed website HTTPS certificates are valid for **365 days**. Existing certificates keep their original expiry until renewal. Run “查看证书有效期” in `data/本站维护说明.txt` to see the expiry dates of the website certificate and the site's root certificate.

**On Synology DSM, set up a daily task once per site.** Follow “群晖自动续签网站证书” in the maintenance notes to create and enable the task, for example at 03:15 each day. It renews the website certificate when 30 days or less remain. The website goes offline briefly during renewal. If DSM email notifications are configured, you can opt to receive task details only on failure. For immediate renewal on DSM, run “续签本站 HTTPS 证书”, then open the original URL and verify the new expiry date. [Synology Task Scheduler](https://kb.synology.com/en-ca/DSM/help/DSM/AdminCenter/system_taskscheduler?version=7)

Sites created with this release receive a root certificate valid through **31 December 9999 at 23:59:59 UTC**. Existing sites keep their original root and expiry date. Renewal uses the same site root, so devices that already trust it can continue to use the original URL. If the root has too little time left to issue a full 365-day certificate, back up and restore to a separate new site before the current website certificate expires. Verify the ledger and trust the new site's root on your devices before switching. Older installations need the release that provides these commands. [Upgrade guide](docs/站主升级.md)

Site owner references in Chinese cover [account recovery](docs/站主找回账号.md) and [upgrades and rollback](docs/站主升级.md).
