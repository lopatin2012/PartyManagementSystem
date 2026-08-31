FROM python:3.14-slim

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
# requirements.txt хранится в UTF-16LE; конвертируем в UTF-8 и выкидываем
# Windows-зависимость pywin32 и самоссылочную git-строку `-e git+...`
# (исходники проекта всё равно попадают в образ через `COPY . .`).
RUN iconv -f utf-16le -t utf-8 requirements.txt | grep -v -e 'pywin32' -e 'git+' > requirements_docker.txt
RUN pip install --no-cache-dir -r requirements_docker.txt

COPY . .

ENV SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0
RUN pip install --no-cache-dir -e .

RUN sed -i "s/'HOST': '127.0.0.1'/'HOST': os.getenv('DB_HOST', '127.0.0.1')/" config/settings.py

EXPOSE 8888

CMD ["sh", "-c", "python app_cz/management/commands/reset_nk_sync.py && python manage.py makemigrations && python manage.py migrate && python manage.py migrate --database archive && python manage.py collectstatic --noinput && python run_all.py"]