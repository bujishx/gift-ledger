# GiftLedger

A self-hosted gift ledger for your LAN or VPN. The initial release supports Linux/amd64 on Ubuntu and Synology Container Manager. [简体中文](README.md)

## Install

Download the [docker-compose.yml](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/docker-compose.yml) into a dedicated empty folder. Set the LAN IP and an available HTTPS port at the top of the file, for example `192.168.1.20` and `8443`. For online installations the server must be able to pull the pinned images. For an offline candidate, verify and import the supplied images according to the package instructions and use the YAML supplied in that same package.

- **Ubuntu** Set the project name in the top-level `name:` field. With Docker Engine and the Compose plugin installed, run `docker compose up -d` in that folder.
- **Synology** In File Station, create a dedicated empty folder such as `/volume1/docker/gift-ledger-family` and put the YAML there. In Container Manager, open **Project → Create**, enter a project name, select that folder and use its existing YAML. You can choose the folder name. Setting `name:` in the YAML to match the Synology project name makes it easier to manage the site from a terminal later.

For a second independent site on the same host, choose a different project name, another available HTTPS port and a separate empty folder. Both sites can use the same LAN IP. By default each site's data and backups are stored under its own folder.

Open `data/网站地址.txt` for the URL and `data/本站维护说明.txt` for the site's project name, instance and ready-to-copy maintenance commands. In an administrator terminal on the server, copy the full command under “启用本站网页维护（每站一次）”. Wait for “本站维护服务已就绪”. On Ubuntu this enables startup at boot. On DSM save the per-site boot task shown in the same section.

Transfer `data/本站根证书.cer` to your phone or Mac and compare its SHA-256 fingerprint before trusting it. The “核对公开根证书” block reads the Docker log over SSH and checks the certificate file on the server, including when the DSM log viewer is empty. A second download may have `(1)` in the filename. Match the fingerprint. Connect to your LAN or VPN, open the site, register, finish your profile and record a gift.

On a Mac, import the root certificate whose fingerprint you verified into your login keychain. In Keychain Access, double-click this site's `GiftLedger Site CA` certificate in the Certificates list. Expand Trust and set Secure Sockets Layer (SSL) to Always Trust. Close the window, confirm the change if prompted, then open the site URL. [Apple’s Keychain Access guide](https://support.apple.com/guide/keychain-access/kyca11871/mac)

Restart the server and confirm the same account still shows that gift. Contact the site owner if you forget your login name or password. The installed YAML pins the image digests, so the site owner chooses when to upgrade.

## Back up and restore in a browser

1. **Sign in as the site owner.** Open the address ending in `/maintenance` in a Windows, Mac or Ubuntu desktop browser. It uses the same IP and port as the ledger. Run the command under “站主维护登录码” in the server's administrator terminal, check the project and URL and enter the short-lived code. You can also set an owner password or reset it using a fresh code.
2. **Enroll a key for this site's backups.** Both a site started from scratch and a site restored from an old backup offer “登记旧钥匙” (enroll an existing key) and “生成新钥匙并登记” (generate and enroll a new key). Select the original key file on your computer or USB drive, such as `identity.txt` or an `.agekey` file. For a new key, download it and select the saved file. Confirm and wait for “本站备份钥匙登记通过”.
3. **Start a backup.** Choose “开始备份”. The site briefly goes offline. Return to the same page, choose “查看备份结果”, and download the verified ZIP. Store the ZIP and its original key separately. Sign in to the ledger and check the last transaction and totals.
4. **Restore an old backup to a separate, empty site.** Use a different project name, empty directory and available port. Before registering any account, open the **target** site's maintenance page, select the source backup and its matching original key, and choose “上传并预检”. Check the source, entry count, target URL and project before entering “恢复” and confirming. Use the original account to check the restored ledger. For the first daily backup of the site restored from the old backup, enroll your choice of the same key or a new one. [Recovery guide](docs/从备份恢复.md)

If the site stays offline, use the command under “网页暂时打不开时查看本站维护任务” in its server-side maintenance notes. Use the original operation ID to check the job and follow the recovery instructions. The notes also include terminal backup commands and optional same-version desktop tools for enrollment and restoration. [Mac tools](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/mac-site-tools.zip) · [Windows tools](https://github.com/{GITHUB_REPOSITORY}/releases/latest/download/windows-site-tools.zip)

The Synology manual backup supports Container Manager 24.0.2-1543 and 24.0.2-1606. It checks the package files at startup. If this check returns `CM_vendor_file_set_unreviewed`, the task exits before stopping the site. Report the Container Manager version shown in Package Center and the run result from Task Scheduler.

## Certificate expiry and renewal

New and renewed website HTTPS certificates are valid for **365 days**. Existing certificates keep their original expiry until renewal. Run “查看证书有效期” in `data/本站维护说明.txt` to see the expiry dates of the website certificate and the site's root certificate.

**On Ubuntu**, enabling web maintenance installs a timer for this site. It checks the certificate daily at 03:15 in the server’s time zone. See “Ubuntu 自动续签网站证书” in the maintenance notes for the next scheduled check and last result.

**On Synology DSM, set up a daily task once per site.** Follow “群晖自动续签网站证书” in the maintenance notes to create and enable the task, for example at 03:15 each day. It renews the website certificate when 30 days or less remain. The website goes offline briefly during renewal. If DSM email notifications are configured, you can opt to receive task details only on failure. For immediate renewal on DSM, run “续签本站 HTTPS 证书”, then open the original URL and verify the new expiry date. [Synology Task Scheduler](https://kb.synology.com/en-ca/DSM/help/DSM/AdminCenter/system_taskscheduler?version=7)

Sites created with this release receive a root certificate valid through **31 December 9999 at 23:59:59 UTC**. Existing sites keep their original root and expiry date. Renewal uses the same site root, so devices that already trust it can continue to use the original URL. If the root has too little time left to issue a full 365-day certificate, back up and restore to a separate new site before the current website certificate expires. Verify the ledger and trust the new site's root on your devices before switching. Older installations need the release that provides these commands. [Upgrade guide](docs/站主升级.md)

Site owner references in Chinese cover [account recovery](docs/站主找回账号.md) and [upgrades and rollback](docs/站主升级.md).
