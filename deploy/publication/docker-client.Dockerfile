ARG BASE
FROM ${BASE}
USER 0:0
# Only the client and its license enter the backup image. The digest matches
# the official static archive used for the field-tested client.
ADD --checksum=sha256:9ac225999fd5e863f7694c728ef23d480616b1e5639328fa309befd4243d69eb https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz /tmp/docker-client.tgz
RUN mkdir -p /usr/local/share/gift-ledger-client-notices && python3.12 -c "import tarfile,shutil; a=tarfile.open('/tmp/docker-client.tgz'); m=a.getmember('docker/docker'); assert m.isfile(); shutil.copyfileobj(a.extractfile(m),open('/usr/bin/docker','wb'))" && chmod 755 /usr/bin/docker && rm /tmp/docker-client.tgz
ADD --checksum=sha256:2d81ea060825006fc8f3fe28aa5dc0ffeb80faf325b612c955229157b8c10dc0 https://raw.githubusercontent.com/docker/cli/v29.9.2/LICENSE /usr/local/share/gift-ledger-client-notices/docker-cli-LICENSE
RUN chmod 644 /usr/local/share/gift-ledger-client-notices/docker-cli-LICENSE && /usr/bin/docker --version
