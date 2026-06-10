# Use official Python slim base image
FROM python:3.12-slim

# Install system dependencies (FFmpeg for media, LibreOffice for DOCX conversion, curl for health checks, git & fontconfig for fonts)
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libreoffice \
    curl \
    git \
    fontconfig \
    && rm -rf /var/lib/apt/lists/*

# Clone and install standard Persian/Farsi fonts (IRFonts, Vazirmatn, and traditional B-Nazanin/B-Zar)
RUN mkdir -p /usr/share/fonts/truetype/persian && \
    git clone --depth 1 https://github.com/farsi-fonts/IRFonts.git /tmp/irfonts && \
    cp /tmp/irfonts/*.ttf /usr/share/fonts/truetype/persian/ && \
    cp /tmp/irfonts/*.TTF /usr/share/fonts/truetype/persian/ || true && \
    rm -rf /tmp/irfonts && \
    git clone --depth 1 https://github.com/rastikerdar/vazirmatn.git /tmp/vazirmatn && \
    find /tmp/vazirmatn -name "*.ttf" -exec cp {} /usr/share/fonts/truetype/persian/ \; && \
    rm -rf /tmp/vazirmatn && \
    git clone --depth 1 https://github.com/SMotlaq/bsc-thesis.git /tmp/bsc && \
    find /tmp/bsc -name "*.ttf" -o -name "*.TTF" -exec cp {} /usr/share/fonts/truetype/persian/ \; && \
    rm -rf /tmp/bsc && \
    fc-cache -fv

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
