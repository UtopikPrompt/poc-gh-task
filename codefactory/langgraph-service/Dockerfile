FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml ./pyproject.toml
COPY src/ ./src/
RUN pip install --no-cache-dir --disable-pip-version-check . \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app

USER 10001:10001
EXPOSE 8001
ENTRYPOINT ["uvicorn", "langgraph_service.host:create_app", "--factory", "--host", "0.0.0.0", "--port", "8001"]