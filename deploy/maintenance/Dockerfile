ARG API_IMAGE=gift-ledger-lan-review-api:latest
FROM ${API_IMAGE}

# The host runner checks the application's exact image ID before this build.
# This is a separate maintenance image; it never replaces the website image.
USER 0:0
COPY deploy/maintenance/install_age.py /opt/maintenance/install_age.py
RUN python3.12 /opt/maintenance/install_age.py
COPY backend/maintenance/ /app/backend/maintenance/
ENV PYTHONDONTWRITEBYTECODE=1 MAINTENANCE_SCOPE=isolated-rehearsal
USER 10001:0
ENTRYPOINT ["python3.12", "-m", "maintenance.cli"]
CMD ["--help"]
