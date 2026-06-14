# MEC-LifeGuard — Edge Node Dockerfile
# Builds a lightweight Python container running the Flask edge node app.
# Each node is the same image; NODE_ID env variable differentiates them.

FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Copy requirements first (Docker layer cache: only re-installs on change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the Flask app
COPY app.py .

# Expose Flask's internal port (mapped to 5001/5002/5003 on the host)
EXPOSE 5000

# Run the app (non-root is best practice; ok to keep root for course project)
CMD ["python", "app.py"]
