FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (semgrep needs these)
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Default command (overridden by docker-compose per service)
ENV PORT=8501
RUN chmod +x start.sh
CMD ["./start.sh"]
