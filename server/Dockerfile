FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home quota && mkdir /data && chown quota:quota /data
COPY app.py detection.py analytics.py browser_auth.py account_management.py bark.py tibo.py insights.py ./
COPY static ./static
USER 10001:10001
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz',timeout=3)"
CMD ["gunicorn", "--bind", "0.0.0.0:8765", "--workers", "1", "--threads", "4", "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "app:create_app()"]
