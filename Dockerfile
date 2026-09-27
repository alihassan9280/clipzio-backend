FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Always build with the newest yt-dlp. Docker caches the layer above while
# requirements.txt is unchanged, so without this a redeploy would keep an old
# yt-dlp. ADD of a URL re-runs this step whenever a new release is published.
ADD https://pypi.org/pypi/yt-dlp/json /tmp/yt-dlp-release.json
RUN pip install --no-cache-dir -U yt-dlp

COPY . .

# Don't run the server as root.
RUN useradd --create-home app && chown -R app /app
USER app

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
