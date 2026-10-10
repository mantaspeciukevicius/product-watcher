FROM python:3.12-slim
WORKDIR /srv
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && playwright install --with-deps chromium && useradd -u 1000 -m tracker
COPY app app
COPY tests tests
COPY browser-extension browser-extension
USER tracker
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765", "--no-proxy-headers"]
