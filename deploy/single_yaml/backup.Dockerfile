ARG CLIENTS=gift-ledger-ops-panel:0.4.0-d17-independent-20261107
ARG BASE=giftledger-candidate/installer:1.5.0-stage2-r1
FROM ${CLIENTS} AS clients
FROM ${BASE} AS dsm-host-tool
COPY backend/app/services/maintenance_enrollment_protocol.py backend/app/services/maintenance_restore_protocol.py /tmp/dsm-build/backend/app/services/
COPY deploy/single_yaml/ /tmp/dsm-build/deploy/single_yaml/
COPY deploy/release/backup_ops/ /tmp/dsm-build/deploy/release/backup_ops/
RUN /opt/venv/bin/python3.12 -I /tmp/dsm-build/deploy/single_yaml/build_dsm_backup.py /tmp/dsm-backup.pyz \
    && /opt/venv/bin/python3.12 -I /tmp/dsm-build/deploy/single_yaml/build_maintenance_install.py /tmp/dsm-backup.pyz /tmp/maintenance-install.pyz
FROM ${BASE}
COPY --from=dsm-host-tool /tmp/maintenance-install.pyz /opt/gift-ledger-install/maintenance-install.pyz
COPY --from=dsm-host-tool /tmp/dsm-backup.pyz /opt/gift-ledger-install/dsm-backup.pyz
COPY --from=clients /usr/bin/docker /usr/bin/docker
COPY --from=clients /usr/local/share/gift-ledger-client-notices/ /usr/local/share/gift-ledger-client-notices/
COPY deploy/release/backup_ops/backup_ops.py deploy/release/backup_ops/backup_worker.py deploy/release/backup_ops/backup_store.py deploy/release/backup_ops/backup_export.py deploy/release/backup_ops/windows_path_budget.py deploy/release/backup_ops/preview_policy.py deploy/release/backup_ops/resource_meter.py /opt/gift-ledger-backup-ops/
COPY deploy/release/gui_v2/maintenance.py deploy/release/gui_v2/certificate_renewal.py deploy/release/gui_v2/key_transfer.py /opt/gift-ledger-gui-v2/
COPY deploy/release/backup_ops/restore_file_check.py /opt/gift-ledger-backup-ops/
COPY deploy/release/gui_v2/restore_receipt_check.py /opt/gift-ledger-gui-v2/
COPY deploy/single_yaml/backup_site.py deploy/single_yaml/backup_key.py deploy/single_yaml/key_lifecycle_worker.py deploy/single_yaml/service_images.json /opt/gift-ledger-install/
COPY deploy/single_yaml/certificate_site.py deploy/single_yaml/certificate_auto.py deploy/single_yaml/certificate_policy.py deploy/single_yaml/site_ca.py /opt/gift-ledger-install/
COPY deploy/single_yaml/site_about.py deploy/single_yaml/restore_site.py deploy/single_yaml/restore_worker.py deploy/single_yaml/restore_input.py deploy/single_yaml/restore_files.py /opt/gift-ledger-install/
ENV PYTHONPATH=/opt/gift-ledger-backup-ops:/opt/gift-ledger-install:/opt/gift-ledger-gui-v2:/app/backend
ENTRYPOINT ["/opt/venv/bin/python3.12", "/opt/gift-ledger-install/backup_site.py"]
