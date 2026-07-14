FROM python:3.12-slim

WORKDIR /operator

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY src/ ./src/

# Run as non-root
RUN useradd -u 1000 -m sherlock
USER 1000

ENTRYPOINT ["python", "main.py"]
