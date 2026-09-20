FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app.py pharmacy_functions.py config.json ./

EXPOSE 8080

CMD ["gunicorn", "-w", "1", "--threads", "50", "-b", "0.0.0.0:8080", "app:app"]
