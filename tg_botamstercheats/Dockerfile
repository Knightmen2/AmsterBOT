FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
ENV DATA_DIR=/var/data

EXPOSE 10000

CMD ["python", "-u", "main.py"]
