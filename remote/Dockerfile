FROM python:3.11-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY remote/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && \
    mkdir /data && chown 10001:10001 /data
COPY remote/*.py remote/
COPY micnaga/ micnaga/
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "remote.server:from_env", "--factory", "--host", "0.0.0.0", "--no-access-log", "--limit-concurrency", "20", "--timeout-graceful-shutdown", "15"]
