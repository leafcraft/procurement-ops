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
COPY agency/ agency/
COPY main.py .

EXPOSE 18820

# A cloud deploy passes the team's configuration (LEAFMESH_TEAM_CONFIG_GZ,
# gzip + base64): it is loaded into the store only while the store is empty,
# then the agency starts. Locally `create-leafmesh --start` loads it instead.
CMD ["sh", "-c", "if [ -n \"$LEAFMESH_TEAM_CONFIG_GZ\" ]; then printf '%s' \"$LEAFMESH_TEAM_CONFIG_GZ\" | base64 -d | gunzip | leafmesh config import --if-empty - || exit 1; fi; exec python main.py"]
