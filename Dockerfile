FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unzip && rm -rf /var/lib/apt/lists/*
COPY quant_signal_v3.7.40_package.bin /tmp/quant_signal_package.zip
RUN unzip -q /tmp/quant_signal_package.zip -d /app && rm -f /tmp/quant_signal_package.zip
RUN pip install --no-cache-dir -r requirements.txt
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
