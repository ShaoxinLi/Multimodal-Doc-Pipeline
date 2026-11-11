FROM ghcr.io/astral-sh/uv:0.9.17 AS uv
FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN groupadd --gid 10001 pipeline && useradd --uid 10001 --gid pipeline --create-home pipeline \
    && mkdir /in /out /config && chown pipeline:pipeline /out
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
WORKDIR /out
ENTRYPOINT ["doc-pipeline"]
CMD ["--help"]
