# Hugging Face Space build: fetches a pinned commit of the GitHub repo, so the
# Space itself only holds this file and README.md. To deploy a new version,
# change REF to the new commit SHA.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash curl procps \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 user \
 && mkdir /app && chown user:user /app
USER user
ENV HOME=/home/user PYTHONUNBUFFERED=1 PORT=7860 DATA_DIR=/data
WORKDIR /app

ARG REPO=SoulFireMage/openlogz
ARG REF=__REF__
RUN curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${REF}" | tar xz --strip-components=1 \
 && rm -rf tests space

EXPOSE 7860
CMD ["sh", "-c", "exec python server.py"]
