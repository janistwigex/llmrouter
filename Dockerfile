FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY router.py .
EXPOSE 4000
# Single worker on purpose: routing state lives in memory.
CMD ["uvicorn", "router:app", "--host", "0.0.0.0", "--port", "4000", "--workers", "1"]

