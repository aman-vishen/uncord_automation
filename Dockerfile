FROM python:3.12-slim
WORKDIR /app
COPY mes_dashboard ./mes_dashboard
COPY server ./server
EXPOSE 8080
CMD ["python", "mes_dashboard/app.py"]
