FROM python:3.13-slim AS base

WORKDIR /app

# System dependencies for compiled packages (grpcio, etc.)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY configs/ configs/
COPY agency/ agency/
COPY main.py .

EXPOSE 18820

CMD ["python", "main.py"]
