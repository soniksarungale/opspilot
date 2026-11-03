FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /srv/opspilot
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY app ./app
COPY data/sample_emails.json ./data/sample_emails.json
ENV DB_PATH=/srv/opspilot/data/opspilot.db
EXPOSE 8000
CMD ["uv", "run", "--no-dev", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
