# Built and pushed from the workbench (spec §8); deploys are image-tag bumps.
# python:3.12-slim is the house Python base from the gateway sign-off (spec
# §12); uv is pinned by tag. The app runs from a prebuilt venv as an
# unprivileged user; the image ships no test tooling.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.10 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
RUN useradd -r -u 10001 gateway
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/src ./src
USER 10001
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["gateway"]
