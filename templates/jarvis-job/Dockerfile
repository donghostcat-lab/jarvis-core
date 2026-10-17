FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY jarvis_job ./jarvis_job
COPY skills ./skills

RUN useradd --create-home --uid 1000 jarvis && mkdir -p /data && chown jarvis /data
USER jarvis
CMD ["python", "-m", "jarvis_job.cli"]
