FROM python:3.14-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN iconv -f utf-16le -t utf-8 requirements.txt | grep -v 'pywin32' > requirements_docker.txt
RUN pip install --no-cache-dir -r requirements_docker.txt

COPY . .

RUN sed -i "s/'HOST': '127.0.0.1'/'HOST': os.getenv('DB_HOST', '127.0.0.1')/" config/settings.py

EXPOSE 8888

CMD ["sh", "-c", "python manage.py migrate && python run_all.py"]