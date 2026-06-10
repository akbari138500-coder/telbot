# Use official Python slim base image
FROM python:3.12-slim

# Install system dependencies (FFmpeg for media, LibreOffice for DOCX conversion, curl for health checks)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libreoffice \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements file first to leverage Docker cache
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application files
COPY . .

# Expose port for keep-alive web server
EXPOSE 8080

# Command to run the bot
CMD ["python", "bot.py"]
