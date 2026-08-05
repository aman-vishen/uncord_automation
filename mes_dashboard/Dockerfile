FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY mes_dashboard/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY mes_dashboard ./mes_dashboard

EXPOSE 10000
CMD ["python", "mes_dashboard/app.py"]
