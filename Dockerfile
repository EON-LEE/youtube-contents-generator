# Render/upload worker for the Azure Container Apps Job.
# FFmpeg comes from imageio-ffmpeg's bundled binary (the code never calls a system
# ffmpeg), so no ffmpeg apt package is installed. fonts-noto-cjk is REQUIRED:
# story_pipeline.fonts resolves /usr/share/fonts/opentype/noto/NotoSansCJK-*.ttc for
# Korean illustrations and burned-in subtitles; fontconfig lets libass find it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STORY_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc \
    STORY_FONT_BOLD=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc \
    STUDIO_LIBRARY_DIR=/app/.story-pipeline/library

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk fontconfig ca-certificates \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 studio \
    && useradd --system --uid 10001 --gid studio --create-home --home-dir /home/studio studio

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[media,studio,youtube]"

COPY studio.toml ./
COPY channel ./channel
RUN mkdir -p /app/.story-pipeline /app/outputs && chown -R studio:studio /app/.story-pipeline /app/outputs

USER studio
ENTRYPOINT ["python", "-m", "story_pipeline", "studio"]
CMD ["job"]
