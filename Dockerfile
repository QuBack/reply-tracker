FROM python:3.13-slim-bookworm AS web
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WEB_DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app \
    && mkdir /data && chown app:app /data
COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt
COPY automation/ automation/
COPY webapp/ webapp/
COPY .agents/skills/supplier-discovery/SKILL.md .agents/skills/supplier-discovery/SKILL.md
USER app
EXPOSE 8080
CMD ["python", "-m", "webapp", "serve", "--host", "0.0.0.0"]

FROM node:22-bookworm-slim AS node-runtime

FROM web AS search
USER root
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g --include=optional @openai/codex \
    && mkdir /codex && chown app:app /codex
ENV CODEX_HOME=/codex
USER app
EXPOSE 8090
CMD ["python", "-m", "webapp.search_gateway"]
