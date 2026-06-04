FROM python:3.11-slim

# Install system dependencies for GUI & OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxtst6 \
    python3-tk \
    libgl1-mesa-dri \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements (web version includes flask)
COPY requirements.txt requirements_web.txt .
RUN pip install --no-cache-dir -r requirements_web.txt

# Copy source code
COPY . .

# Expose web server port
EXPOSE 5000

# Run the web server
CMD ["python", "web_server.py"]
