FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SCHOLAR_API_HOST=0.0.0.0 \
    SCHOLAR_DB_PATH=/data/scholar.sqlite3

RUN addgroup --system scholar && adduser --system --ingroup scholar scholar
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[api,mcp,distributed]"
RUN mkdir /data && chown scholar:scholar /data

USER scholar
EXPOSE 8000 8001
CMD ["scholar-api"]
