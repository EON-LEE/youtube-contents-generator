# Foundry hosted agent "story-studio" (Invocations protocol 2.0.0, port 8088).
# Build from the repository root:  az acr build -r <acr> -t story-studio:<tag> -f infra/hosted-agent/Dockerfile .
# The platform requires linux/amd64 images.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STUDIO_RUNS_DIR=/app/.story-pipeline/studio-runs

RUN groupadd --system --gid 10001 studio \
    && useradd --system --uid 10001 --gid studio --create-home --home-dir /home/studio studio

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# agent-framework-foundry-hosting is pre-release; --pre lets pip resolve it.
RUN pip install --pre ".[studio,hosted]"

COPY studio.toml ./
COPY channel ./channel
RUN mkdir -p /app/.story-pipeline && chown -R studio:studio /app/.story-pipeline

USER studio
EXPOSE 8088
CMD ["python", "-m", "story_pipeline.studio.hosted"]
