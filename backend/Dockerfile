FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt 
COPY main2.py prompt.py .

CMD ["python", "main2.py"]
