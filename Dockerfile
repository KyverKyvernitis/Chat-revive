FROM python:3.11-slim

# ffmpeg + opus (voz do Discord) + libsodium (PyNaCl)
RUN apt-get update \
 && apt-get install -y ffmpeg libopus0 libsodium23 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
CMD ["python", "bot.py"]
