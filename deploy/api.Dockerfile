FROM mysql:8.4.11-oraclelinux9

# 同一官方 MySQL 8.4 镜像提供 mysql 和 mysqldump，管理员整库备份可直接调用。
RUN microdnf install -y python3.12 python3.12-pip python3.12-setuptools python3.12-wheel \
    && microdnf clean all \
    && python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /app/backend
COPY backend/requirements.lock /tmp/requirements.lock
RUN /opt/venv/bin/python -m pip install --no-cache-dir -r /tmp/requirements.lock \
    && mkdir -p /var/lib/gift-ledger \
    && chown -R 10001:0 /var/lib/gift-ledger

COPY --chown=10001:0 backend/ /app/backend/
COPY --chown=10001:0 deploy/api_entrypoint.py /app/deploy/api_entrypoint.py

ARG GIFTLEDGER_BUILD_COMMIT
ARG GIFTLEDGER_BUILD_DATE
ENV GIFTLEDGER_BUILD_COMMIT=${GIFTLEDGER_BUILD_COMMIT} GIFTLEDGER_BUILD_DATE=${GIFTLEDGER_BUILD_DATE}
USER 10001:0
EXPOSE 8000
ENTRYPOINT ["python3.12", "/app/deploy/api_entrypoint.py"]
CMD ["python3.12", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
