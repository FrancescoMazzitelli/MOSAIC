FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN apt-get update && apt-get install -y curl
RUN pip install --default-timeout=1000 --no-cache-dir -r requirements.txt
# Copy the entire local folder content, preserving original names
COPY . .

EXPOSE 5000

CMD ["python", "db-gateway.py"]