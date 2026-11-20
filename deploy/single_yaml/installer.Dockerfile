ARG BASE=giftledger-website-maintenance:s011-logfix-b7158b550d9d
FROM ${BASE}
COPY deploy/release/gui/prepare.py deploy/release/gui/initialize.py /opt/gift-ledger-gui/
COPY deploy/release/gui_v2/prepare_volumes.py deploy/release/gui_v2/initialize_volumes.py /opt/gift-ledger-gui-v2/
COPY deploy/release/assets/schema_v2_s004.sql deploy/release/assets/schema_v2_s010.sql deploy/release/assets/schema_v2_s011.sql deploy/release/assets/verify_schema.py deploy/release/assets/SHA256SUMS /opt/gift-ledger-release/
COPY deploy/single_yaml/single_yaml_site.py deploy/single_yaml/site_ca.py deploy/single_yaml/certificate_policy.py deploy/single_yaml/owner_guide.py deploy/single_yaml/owner_login_code.py /opt/gift-ledger-install/
COPY deploy/single_yaml/station_guides.py deploy/single_yaml/安装前检查端口与网络.txt /opt/gift-ledger-install/
ENV PYTHONPATH=/opt/gift-ledger-install:/opt/gift-ledger-gui-v2:/app/backend GIFTLEDGER_INSTALL_KIND=single-yaml
ENTRYPOINT ["/opt/venv/bin/python3.12", "/opt/gift-ledger-install/single_yaml_site.py"]
