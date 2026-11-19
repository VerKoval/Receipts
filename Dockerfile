# Cloud Run runs this. Buildpacks would also work, but a Dockerfile makes the Python version
# and the uv lock explicit, which is one less thing to debug the night before a deadline.
FROM python:3.12-slim

# uv resolves from uv.lock, so the deploy installs exactly what you tested against.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

# Cloud Run sets PORT. uvicorn must bind 0.0.0.0, not localhost.
ENV PORT=8080
CMD ["uv", "run", "--no-sync", "python", "app.py"]
